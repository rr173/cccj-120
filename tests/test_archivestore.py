"""Regression tests for archivestore.  Run: python3 -m pytest tests/ -q"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from archivestore import ArchiveStore, generate_key_file  # noqa: E402
from archivestore.errors import (  # noqa: E402
    AuthenticationError,
    BlobStateError,
    KeyUnavailableError,
    RangeError,
    RotationError,
    TargetExistsError,
)
from archivestore.layout import TAG_LEN  # noqa: E402

B = 256  # small block size so tests exercise many blocks


@pytest.fixture()
def env(tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    k1 = keys / "k1.key"
    k2 = keys / "k2.key"
    generate_key_file(k1)
    generate_key_file(k2)
    storage = tmp_path / "storage"
    src = tmp_path / "src.bin"
    data = os.urandom(700) + b"\0" * 300 + os.urandom(500)  # 1500 bytes
    src.write_bytes(data)
    with ArchiveStore(storage, {"k1": k1, "k2": k2}) as store:
        aid = store.seal(src, key_id="k1", block_size=B)
    return tmp_path, storage, k1, k2, src, data, aid


def read(storage, keys, aid, s, e, tmp_path, name="out.bin"):
    out = tmp_path / name
    with ArchiveStore(storage, keys) as store:
        stats = store.read_range(aid, s, e, out)
    return out.read_bytes(), stats


def test_roundtrip_ranges(env):
    tmp_path, storage, k1, k2, src, data, aid = env
    keys = {"k1": k1}
    n = len(data)
    for s, e in [(0, n), (0, 1), (0, 100), (250, 600), (n - 10, n), (5, 5), (0, 0)]:
        got, stats = read(storage, keys, aid, s, e, tmp_path, f"o-{s}-{e}.bin")
        assert got == data[s:e]
        assert stats.bytes_written == e - s
    # cross-block read touches exactly the covering blocks
    _, stats = read(storage, keys, aid, B - 10, B + 10, tmp_path, "x.bin")
    assert stats.blocks_read == 2
    assert stats.cipher_bytes_read == 2 * (B + TAG_LEN)


def test_illegal_ranges(env):
    tmp_path, storage, k1, k2, src, data, aid = env
    with ArchiveStore(storage, {"k1": k1}) as store:
        for s, e in [(-1, 5), (5, 2), (0, len(data) + 1), (1.5, 2)]:
            with pytest.raises((RangeError, TypeError)):
                store.read_range(aid, s, e, tmp_path / "bad.bin")


def test_empty_archive(env):
    tmp_path, storage, k1, k2, src, data, aid = env
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    with ArchiveStore(storage, {"k1": k1}) as store:
        eid = store.seal(empty, key_id="k1", block_size=B)
        stats = store.verify(eid)
        assert stats.blocks_checked == 0
        out = tmp_path / "empty-out.bin"
        store.read_range(eid, 0, 0, out)
        assert out.read_bytes() == b""
        with pytest.raises(RangeError):
            store.read_range(eid, 0, 1, tmp_path / "nope.bin")


def test_verify_ok_and_stats(env):
    tmp_path, storage, k1, k2, src, data, aid = env
    with ArchiveStore(storage, {"k1": k1}) as store:
        stats = store.verify(aid)
    assert stats.blocks_checked == 6  # ceil(1500/256)
    assert stats.bytes_checked == len(data)


def _blob(storage, aid) -> Path:
    return storage / "blobs" / f"{aid}.bin"


def test_swap_blocks_detected(env):
    tmp_path, storage, k1, k2, src, data, aid = env
    p = _blob(storage, aid)
    d = bytearray(p.read_bytes())
    bs = B + TAG_LEN
    d[bs:3 * bs] = d[2 * bs:3 * bs] + d[bs:2 * bs]
    p.write_bytes(bytes(d))
    with ArchiveStore(storage, {"k1": k1}) as store:
        with pytest.raises(AuthenticationError):
            store.verify(aid)
        with pytest.raises(AuthenticationError):
            store.read_range(aid, B, 2 * B, tmp_path / "x.bin")
        # untouched region still readable
        store.read_range(aid, 0, 10, tmp_path / "ok.bin")
        assert (tmp_path / "ok.bin").read_bytes() == data[:10]


def test_descriptor_tamper_detected(env):
    tmp_path, storage, k1, k2, src, data, aid = env
    con = sqlite3.connect(storage / "archive.db")
    raw = bytearray(con.execute("SELECT descriptor FROM archives WHERE archive_id=?",
                                (bytes.fromhex(aid),)).fetchone()[0])
    raw[29] ^= 1
    con.execute("UPDATE archives SET descriptor=? WHERE archive_id=?",
                (bytes(raw), bytes.fromhex(aid)))
    con.commit(); con.close()
    with ArchiveStore(storage, {"k1": k1}) as store:
        with pytest.raises(AuthenticationError):
            store.verify(aid)


def test_truncate_and_append_detected(env):
    tmp_path, storage, k1, k2, src, data, aid = env
    for mutate in (lambda b: b[:-50], lambda b: b + os.urandom(32)):
        p = _blob(storage, aid)
        orig = p.read_bytes()
        p.write_bytes(mutate(orig))
        with ArchiveStore(storage, {"k1": k1}) as store:
            with pytest.raises(BlobStateError):
                store.verify(aid)
            with pytest.raises(BlobStateError):
                store.read_range(aid, 0, 5, tmp_path / "x.bin")
        p.write_bytes(orig)  # restore for the second case


def test_wrong_key_and_missing_key(env):
    tmp_path, storage, k1, k2, src, data, aid = env
    with ArchiveStore(storage, {"k1": k2}) as store:  # k2's bytes under k1's id
        with pytest.raises(AuthenticationError):
            store.verify(aid)
    with ArchiveStore(storage, {}) as store:
        with pytest.raises(KeyUnavailableError):
            store.verify(aid)


def test_failed_read_preserves_target(env):
    tmp_path, storage, k1, k2, src, data, aid = env
    p = _blob(storage, aid)
    d = bytearray(p.read_bytes())
    d[5] ^= 0xFF  # corrupt block 1
    p.write_bytes(bytes(d))
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"important")
    with ArchiveStore(storage, {"k1": k1}) as store:
        with pytest.raises(AuthenticationError):
            store.read_range(aid, 0, 10, victim)
    assert victim.read_bytes() == b"important"
    # and no scratch files are left behind
    assert list(tmp_path.glob("*.part")) == []


def test_rotate_roundtrip(env):
    tmp_path, storage, k1, k2, src, data, aid = env
    blob_before = _blob(storage, aid).read_bytes()
    with ArchiveStore(storage, {"k1": k1, "k2": k2}) as store:
        assert store.rotate(old_key_id="k1", new_key_id="k2") == 1
    assert _blob(storage, aid).read_bytes() == blob_before
    with ArchiveStore(storage, {"k2": k2}) as store:
        assert store.verify(aid).bytes_checked == len(data)
    with ArchiveStore(storage, {"k1": k1}) as store:
        with pytest.raises(KeyUnavailableError):
            store.verify(aid)


def test_rotate_rejects_corrupt_envelope_atomically(env):
    tmp_path, storage, k1, k2, src, data, aid = env
    with ArchiveStore(storage, {"k1": k1}) as store:
        aid2 = store.seal(src, key_id="k1", block_size=B)
    con = sqlite3.connect(storage / "archive.db")
    w = bytearray(con.execute("SELECT wrapped_dek FROM envelopes WHERE archive_id=?",
                              (bytes.fromhex(aid2),)).fetchone()[0])
    w[15] ^= 1
    con.execute("UPDATE envelopes SET wrapped_dek=? WHERE archive_id=?",
                (bytes(w), bytes.fromhex(aid2)))
    con.commit(); con.close()
    with ArchiveStore(storage, {"k1": k1, "k2": k2}) as store:
        with pytest.raises(AuthenticationError):
            store.rotate(old_key_id="k1", new_key_id="k2")
        # nothing moved: the untouched archive still opens with the old key,
        # and every envelope is still recorded under k1
        store.verify(aid)
        kids = {r[0] for r in store._con.execute("SELECT key_id FROM envelopes")}
        assert kids == {"k1"}


def test_rotate_hook_abort_rolls_back(env):
    tmp_path, storage, k1, k2, src, data, aid = env

    class Boom(Exception):
        pass

    def hook(stage):
        if stage == "pre_commit":
            raise Boom()

    with ArchiveStore(storage, {"k1": k1, "k2": k2}) as store:
        with pytest.raises(Boom):
            store.rotate(old_key_id="k1", new_key_id="k2", pause_hook=hook)
        store.verify(aid)  # old generation intact
        kids = {r[0] for r in store._con.execute("SELECT key_id FROM envelopes")}
        assert kids == {"k1"}


def test_reseal_uses_fresh_randomness(env):
    tmp_path, storage, k1, k2, src, data, aid = env
    with ArchiveStore(storage, {"k1": k1}) as store:
        aid_b = store.seal(src, key_id="k1", block_size=B)
    assert aid_b != aid
    assert _blob(storage, aid).read_bytes() != _blob(storage, aid_b).read_bytes()


def test_handle_survives_rotation(env):
    tmp_path, storage, k1, k2, src, data, aid = env
    with ArchiveStore(storage, {"k1": k1, "k2": k2}) as store:
        handle = store.open(aid)          # unwraps DEK with k1
        store.rotate(old_key_id="k1", new_key_id="k2")
        out = tmp_path / "held.bin"
        handle.read_range(100, 200, out)  # still works with the held DEK
        assert out.read_bytes() == data[100:200]
        handle.close()


def test_no_plaintext_or_dek_in_catalogue(env):
    tmp_path, storage, k1, k2, src, data, aid = env
    db = (storage / "archive.db").read_bytes()
    assert data[:64] not in db
    assert data[:64] not in _blob(storage, aid).read_bytes()


def test_orphan_blob_and_stale_tmp_are_inert(env):
    """A crash between blob rename and catalogue registration must not create
    a receivable archive, and maintenance must never touch other archives."""
    tmp_path, storage, k1, k2, src, data, aid = env
    # orphan blob: looks like ciphertext, but has no catalogue row
    orphan = storage / "blobs" / ("0" * 32 + ".bin")
    orphan.write_bytes(os.urandom(4096))
    # stale scratch files in tmp/
    old_part = storage / "tmp" / "seal-1-deadbeef.part"
    old_part.write_bytes(os.urandom(100))
    old_time = 1_000_000_000
    os.utime(old_part, (old_time, old_time))
    fresh_part = storage / "tmp" / "seal-2-feedface.part"
    fresh_part.write_bytes(os.urandom(100))
    other = storage / "tmp" / "unrelated.txt"
    other.write_bytes(b"do not touch")
    with ArchiveStore(storage, {"k1": k1}) as store:
        assert store.list_archives() == [aid]  # orphan not receivable
        assert store.verify(aid).bytes_checked == len(data)
        removed = store.cleanup_stale_tmp(older_than_seconds=3600)
        assert removed == 1
        assert not old_part.exists()          # stale scratch removed
        assert fresh_part.exists()            # recent scratch kept
        assert other.read_bytes() == b"do not touch"  # foreign files untouched
        assert orphan.exists()                # blobs/ is never swept
        assert store.verify(aid).bytes_checked == len(data)
