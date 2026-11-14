"""Encrypted archive storage: seal / read-range / verify / rotate.

Layout of a storage directory:

    <storage>/
        archive.db        SQLite catalogue (descriptors, tags, envelopes)
        blobs/<id>.bin    ciphertext blocks, one file per archive
        tmp/              scratch space for in-progress seal/read operations

The catalogue never stores plaintext DEKs or KEKs; key files live outside
the storage directory and are supplied through a KeyFileProvider.  Whoever
copies `storage/` and `archive.db` without the key files cannot recover any
plaintext.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Union

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import layout
from .errors import (
    ArchiveNotFoundError,
    AuthenticationError,
    BlobStateError,
    RangeError,
    RotationError,
    SourceChangedError,
    TargetExistsError,
)
from .keys import KeyFileProvider

log = logging.getLogger("archivestore")

PathLike = Union[str, os.PathLike]

SCHEMA = """
CREATE TABLE IF NOT EXISTS archives (
    archive_id   BLOB PRIMARY KEY,          -- 16 bytes
    descriptor   BLOB NOT NULL,             -- 78-byte canonical descriptor
    desc_tag     BLOB NOT NULL,             -- 16-byte GCM tag (nonce index 0)
    blob_relpath TEXT NOT NULL,             -- e.g. blobs/<hex>.bin
    created_utc  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS envelopes (
    archive_id  BLOB PRIMARY KEY REFERENCES archives(archive_id) ON DELETE CASCADE,
    key_id      TEXT NOT NULL,              -- which KEK currently wraps the DEK
    wrapped_dek BLOB NOT NULL               -- 60 bytes: nonce || ct || tag
);
"""

# Pause-hook stage names used by rotate(); the demo harness relies on them.
STAGE_PRE_COMMIT = "pre_commit"
STAGE_POST_COMMIT = "post_commit"


@dataclass(frozen=True)
class ReadStats:
    blocks_read: int          # ciphertext blocks actually touched
    cipher_bytes_read: int    # ciphertext+tag bytes actually read from disk
    bytes_written: int        # plaintext bytes written to the target


@dataclass(frozen=True)
class VerifyStats:
    blocks_checked: int
    bytes_checked: int
    sha256_hex: str


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file(f) -> None:
    f.flush()
    os.fsync(f.fileno())


class ArchiveStore:
    """One open view of a storage directory.  Not thread-safe; use one
    instance per thread/process.  SQLite serialises concurrent writers."""

    def __init__(self, storage_dir: PathLike, key_provider):
        self._root = Path(storage_dir)
        self._blobs = self._root / "blobs"
        self._tmp = self._root / "tmp"
        self._blobs.mkdir(parents=True, exist_ok=True)
        self._tmp.mkdir(parents=True, exist_ok=True)
        if isinstance(key_provider, dict):
            key_provider = KeyFileProvider(key_provider)
        self._keys = key_provider
        self._con = sqlite3.connect(self._root / "archive.db", isolation_level=None)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=FULL")
        self._con.execute("PRAGMA foreign_keys=ON")
        self._con.execute("PRAGMA busy_timeout=10000")
        self._con.executescript(SCHEMA)

    # ------------------------------------------------------------------ util

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> "ArchiveStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @staticmethod
    def _normalise_archive_id(archive_id) -> bytes:
        if isinstance(archive_id, str):
            try:
                aid = bytes.fromhex(archive_id)
            except ValueError:
                raise ArchiveNotFoundError(f"malformed archive_id {archive_id!r}") from None
        else:
            aid = bytes(archive_id)
        if len(aid) != layout.ARCHIVE_ID_LEN:
            raise ArchiveNotFoundError("archive_id must be 16 bytes")
        return aid

    @staticmethod
    def _wrap_dek(kek: bytes, key_id: str, archive_id: bytes, desc_digest: bytes,
                  dek: bytes) -> bytes:
        nonce = os.urandom(layout.WRAP_NONCE_LEN)
        aad = layout.envelope_aad(archive_id, desc_digest, key_id)
        return nonce + AESGCM(kek).encrypt(nonce, dek, aad)

    @staticmethod
    def _unwrap_dek(kek: bytes, key_id: str, archive_id: bytes, desc_digest: bytes,
                    wrapped: bytes) -> bytes:
        if len(wrapped) != layout.WRAPPED_DEK_LEN:
            raise AuthenticationError(
                f"wrapped DEK for archive {archive_id.hex()} is malformed"
            )
        nonce, ct = wrapped[: layout.WRAP_NONCE_LEN], wrapped[layout.WRAP_NONCE_LEN:]
        aad = layout.envelope_aad(archive_id, desc_digest, key_id)
        try:
            return AESGCM(kek).decrypt(nonce, ct, aad)
        except InvalidTag:
            raise AuthenticationError(
                f"DEK unwrap failed for archive {archive_id.hex()} "
                f"(wrong KEK, corrupted envelope, or corrupted descriptor)"
            ) from None

    @staticmethod
    def _check_desc_tag(aead: AESGCM, desc: layout.Descriptor, desc_tag: bytes) -> None:
        nonce = layout.content_nonce(desc.nonce_prefix, layout.DESC_AUTH_INDEX)
        try:
            aead.decrypt(nonce, desc_tag, desc.raw)
        except InvalidTag:
            raise AuthenticationError(
                f"descriptor authentication failed for archive {desc.archive_id_hex}"
            ) from None

    def _blob_path(self, relpath: str) -> Path:
        p = (self._root / relpath).resolve()
        if p.parent != self._blobs.resolve():
            raise BlobStateError(f"blob path {relpath!r} escapes the blobs directory")
        return p

    # ------------------------------------------------------------------- seal

    def seal(self, source_path: PathLike, *, key_id: str, block_size: int) -> str:
        """Encrypt `source_path` into a new archive; returns the archive_id hex.

        Memory use is O(block_size): the source is streamed twice (a hashing
        pass and an encrypting pass) and never held in memory as a whole.
        The ciphertext becomes visible only after it is fully written,
        fsynced, moved into blobs/ and registered in the catalogue.
        """
        if not isinstance(block_size, int) or not (
            layout.MIN_BLOCK_SIZE <= block_size <= layout.MAX_BLOCK_SIZE
        ):
            raise ValueError(
                f"block_size must be an int in "
                f"[{layout.MIN_BLOCK_SIZE}, {layout.MAX_BLOCK_SIZE}]"
            )
        kek = self._keys.load(key_id)
        src = Path(source_path)
        size = src.stat().st_size

        # Pass 1: hash the original.
        h = hashlib.sha256()
        total = 0
        with open(src, "rb") as f:
            while True:
                chunk = f.read(block_size)
                if not chunk:
                    break
                h.update(chunk)
                total += len(chunk)
        if total != size:
            raise SourceChangedError(
                f"source {src} changed size during seal ({size} -> {total})"
            )
        orig_len = total
        block_count = layout.block_count_for(orig_len, block_size)

        # Fresh random material for every seal; an interrupted seal is never
        # resumed, so its nonce prefix / DEK are never reused.
        dek = os.urandom(layout.DEK_LEN)
        archive_id = os.urandom(layout.ARCHIVE_ID_LEN)
        prefix = os.urandom(layout.NONCE_PREFIX_LEN)
        desc_raw = layout.encode_descriptor(
            archive_id=archive_id,
            orig_len=orig_len,
            block_size=block_size,
            block_count=block_count,
            nonce_prefix=prefix,
            sha256=h.digest(),
        )
        desc = layout.decode_descriptor(desc_raw)
        aead = AESGCM(dek)
        desc_tag = aead.encrypt(
            layout.content_nonce(prefix, layout.DESC_AUTH_INDEX), b"", desc_raw
        )

        blob_rel = f"blobs/{archive_id.hex()}.bin"
        blob_abs = self._blobs / f"{archive_id.hex()}.bin"
        tmp = self._tmp / f"seal-{os.getpid()}-{secrets.token_hex(8)}.part"
        blob_committed = False
        try:
            # Pass 2: encrypt block by block into tmp/, fsync, then rename.
            with open(src, "rb") as fin, open(tmp, "xb") as fout:
                for index in range(layout.FIRST_CONTENT_INDEX, block_count + 1):
                    chunk = fin.read(block_size)
                    if not chunk:
                        raise SourceChangedError(f"source {src} shrank during seal")
                    ct = aead.encrypt(
                        layout.content_nonce(prefix, index),
                        chunk,
                        layout.content_aad(desc.digest, index, len(chunk)),
                    )
                    fout.write(ct)
                if fin.read(1):
                    raise SourceChangedError(f"source {src} grew during seal")
                _fsync_file(fout)
            os.rename(tmp, blob_abs)
            blob_committed = True
            _fsync_dir(self._blobs)

            wrapped = self._wrap_dek(kek, key_id, archive_id, desc.digest, dek)
            now = datetime.now(timezone.utc).isoformat()
            self._con.execute("BEGIN IMMEDIATE")
            try:
                self._con.execute(
                    "INSERT INTO archives(archive_id, descriptor, desc_tag,"
                    " blob_relpath, created_utc) VALUES(?,?,?,?,?)",
                    (archive_id, desc_raw, desc_tag, blob_rel, now),
                )
                self._con.execute(
                    "INSERT INTO envelopes(archive_id, key_id, wrapped_dek)"
                    " VALUES(?,?,?)",
                    (archive_id, key_id, wrapped),
                )
                self._con.execute("COMMIT")
            except Exception:
                self._con.execute("ROLLBACK")
                raise
        except Exception:
            # Best-effort cleanup of artefacts created by *this* seal only.
            # The tmp name and blob name are unique to this attempt, so no
            # other archive can be affected.  An unregistered blob is inert:
            # the catalogue is the only receipt, and it has no row for it.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            if blob_committed:
                try:
                    blob_abs.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        log.info("sealed archive %s (%d bytes, %d blocks, key_id=%s)",
                 archive_id.hex(), orig_len, block_count, key_id)
        return archive_id.hex()

    # ------------------------------------------------------------------- open

    def open(self, archive_id, *, key_id: Optional[str] = None) -> "ArchiveHandle":
        """Unwrap the DEK and authenticate the descriptor; returns a handle.

        The handle keeps the DEK in memory, so it keeps working across a key
        rotation performed by another process.
        """
        aid = self._normalise_archive_id(archive_id)
        row = self._con.execute(
            "SELECT a.descriptor, a.desc_tag, a.blob_relpath, e.key_id, e.wrapped_dek"
            " FROM archives a JOIN envelopes e USING(archive_id)"
            " WHERE a.archive_id = ?",
            (aid,),
        ).fetchone()
        if row is None:
            raise ArchiveNotFoundError(f"archive {aid.hex()} is not registered")
        desc_raw, desc_tag, blob_rel, env_key_id, wrapped = row
        desc = layout.decode_descriptor(desc_raw)
        if desc.archive_id != aid:
            raise AuthenticationError(
                f"catalogue/descriptor archive_id mismatch for {aid.hex()}"
            )
        if key_id is not None and key_id != env_key_id:
            from .errors import KeyUnavailableError

            raise KeyUnavailableError(
                f"archive {aid.hex()} is wrapped with key_id {env_key_id!r}, "
                f"not {key_id!r}"
            )
        kek = self._keys.load(env_key_id)
        dek = self._unwrap_dek(kek, env_key_id, aid, desc.digest, wrapped)
        aead = AESGCM(dek)
        self._check_desc_tag(aead, desc, desc_tag)
        handle = ArchiveHandle(self, desc, dek, desc_tag, self._blob_path(blob_rel))
        handle._check_blob_size()
        return handle

    def read_range(self, archive_id, start: int, end: int, out_path: PathLike,
                   *, key_id: Optional[str] = None) -> ReadStats:
        """One-shot convenience: open, read [start, end), close."""
        with self.open(archive_id, key_id=key_id) as handle:
            return handle.read_range(start, end, out_path)

    def verify(self, archive_id, *, key_id: Optional[str] = None) -> VerifyStats:
        """One-shot convenience: open, verify whole archive, close."""
        with self.open(archive_id, key_id=key_id) as handle:
            return handle.verify()

    # ----------------------------------------------------------------- rotate

    def rotate(self, *, old_key_id: str, new_key_id: str,
               pause_hook: Optional[Callable[[str], None]] = None) -> int:
        """Re-wrap every DEK from old_key_id to new_key_id atomically.

        Ciphertext blobs and descriptors are not touched.  Every old envelope
        and descriptor is authenticated first; if any single one fails, the
        whole round is rejected and the catalogue is left unchanged.  The
        switch is a single SQLite transaction: a crash leaves either all-old
        or all-new envelopes, never a mixture.
        """
        if old_key_id == new_key_id:
            raise ValueError("old_key_id and new_key_id must differ")
        old_kek = self._keys.load(old_key_id)
        new_kek = self._keys.load(new_key_id)

        self._con.execute("BEGIN IMMEDIATE")
        try:
            rows = list(
                self._con.execute(
                    "SELECT a.archive_id, a.descriptor, a.desc_tag,"
                    "       e.key_id, e.wrapped_dek"
                    " FROM archives a JOIN envelopes e USING(archive_id)"
                    " ORDER BY a.archive_id"
                )
            )
            plan = []
            for aid, desc_raw, desc_tag, env_key_id, wrapped in rows:
                if env_key_id != old_key_id:
                    raise RotationError(
                        f"archive {aid.hex()} is wrapped with key_id "
                        f"{env_key_id!r}, expected {old_key_id!r}; "
                        f"refusing the whole rotation"
                    )
                desc = layout.decode_descriptor(desc_raw)
                dek = self._unwrap_dek(old_kek, old_key_id, aid, desc.digest, wrapped)
                self._check_desc_tag(AESGCM(dek), desc, desc_tag)
                plan.append(
                    (aid, self._wrap_dek(new_kek, new_key_id, aid, desc.digest, dek))
                )
            for aid, new_wrapped in plan:
                self._con.execute(
                    "UPDATE envelopes SET key_id = ?, wrapped_dek = ?"
                    " WHERE archive_id = ?",
                    (new_key_id, new_wrapped, aid),
                )
            if pause_hook is not None:
                pause_hook(STAGE_PRE_COMMIT)
            self._con.execute("COMMIT")
        except Exception:
            self._con.execute("ROLLBACK")
            raise
        if pause_hook is not None:
            pause_hook(STAGE_POST_COMMIT)
        log.info("rotated %d envelope(s) from key_id=%s to key_id=%s",
                 len(plan), old_key_id, new_key_id)
        return len(plan)

    # ------------------------------------------------------------- maintenance

    def list_archives(self) -> list[str]:
        return [
            row[0].hex()
            for row in self._con.execute("SELECT archive_id FROM archives ORDER BY 1")
        ]

    def envelope_key_ids(self) -> dict:
        """archive_id hex -> key_id currently wrapping its DEK (for audits)."""
        return {
            row[0].hex(): row[1]
            for row in self._con.execute("SELECT archive_id, key_id FROM envelopes")
        }

    def cleanup_stale_tmp(self, *, older_than_seconds: float = 3600.0) -> int:
        """Remove interrupted-seal scratch files from tmp/.

        Only files matching the seal tmp pattern inside tmp/ are touched,
        and only if they are old enough; blobs/ is never scanned, so no
        registered archive can be affected.
        """
        import time

        removed = 0
        now = time.time()
        for entry in self._tmp.iterdir():
            if not entry.name.startswith("seal-") or not entry.name.endswith(".part"):
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            if now - st.st_mtime < older_than_seconds:
                continue
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
        return removed


class ArchiveHandle:
    """An opened archive: descriptor authenticated, DEK held in memory.

    Reads and verifications through a handle do not consult the catalogue,
    so a handle obtained before a rotation keeps reading the (unchanged)
    ciphertext with the DEK it already unwrapped.
    """

    def __init__(self, store: ArchiveStore, desc: layout.Descriptor, dek: bytes,
                 desc_tag: bytes, blob_path: Path):
        self._store = store
        self._desc = desc
        self._aead = AESGCM(dek)
        self._desc_tag = desc_tag
        self._blob = blob_path

    # -- context manager -------------------------------------------------
    def __enter__(self) -> "ArchiveHandle":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        # Best effort only; secure erasure of process memory is explicitly
        # out of scope (see README "Security boundaries").
        self._aead = None

    # -- properties --------------------------------------------------------
    @property
    def descriptor(self) -> layout.Descriptor:
        return self._desc

    @property
    def archive_id(self) -> str:
        return self._desc.archive_id_hex

    @property
    def length(self) -> int:
        return self._desc.orig_len

    # -- internal ------------------------------------------------------------
    def _check_blob_size(self) -> None:
        try:
            actual = self._blob.stat().st_size
        except FileNotFoundError:
            raise BlobStateError(
                f"blob for archive {self.archive_id} is missing: {self._blob}"
            ) from None
        expected = self._desc.expected_blob_size()
        if actual != expected:
            raise BlobStateError(
                f"blob for archive {self.archive_id} is {actual} bytes, "
                f"expected exactly {expected} (truncated or appended?)"
            )

    def _read_block(self, fd: int, index: int) -> bytes:
        offset, stored_len = self._desc.block_span(index)
        buf = os.pread(fd, stored_len, offset)
        if len(buf) != stored_len:
            raise BlobStateError(
                f"short read on block {index} of archive {self.archive_id}"
            )
        plen = self._desc.block_plain_len(index)
        nonce = layout.content_nonce(self._desc.nonce_prefix, index)
        aad = layout.content_aad(self._desc.digest, index, plen)
        try:
            return self._aead.decrypt(nonce, buf, aad)
        except InvalidTag:
            raise AuthenticationError(
                f"block {index} of archive {self.archive_id} failed authentication"
            ) from None

    # -- public --------------------------------------------------------------
    def iter_blocks(self):
        """Yield (index, plaintext) for every content block, in order.

        Streaming companion to verify(): each block is authenticated and
        decrypted on demand, so at most one block of plaintext is held in
        memory at a time.  Intended for consumers that parse an archive
        sequentially (e.g. the warcdb fact-store importer) without
        materialising the whole plaintext anywhere.
        """
        self._check_blob_size()
        fd = os.open(self._blob, os.O_RDONLY)
        try:
            for index in range(layout.FIRST_CONTENT_INDEX,
                               self._desc.block_count + 1):
                yield index, self._read_block(fd, index)
        finally:
            os.close(fd)

    def read_range(self, start: int, end: int, out_path: PathLike) -> ReadStats:
        """Write plaintext of [start, end) to `out_path` (must not exist).

        Only the ciphertext blocks covering the range are read from disk.
        The output is written to a scratch file first and atomically linked
        into place; on any failure the scratch file is removed and an
        existing target is left untouched.
        """
        if not isinstance(start, int) or not isinstance(end, int):
            raise RangeError("start and end must be integers")
        n = self._desc.orig_len
        if start < 0 or end < start or end > n:
            raise RangeError(
                f"invalid range [{start}, {end}) for archive of length {n}"
            )
        self._check_blob_size()
        out = Path(out_path)
        tmp = out.parent / f".read-{os.getpid()}-{secrets.token_hex(8)}.part"
        blocks = 0
        cipher_bytes = 0
        written = 0
        try:
            b = self._desc.block_size
            with open(tmp, "xb") as fout:
                if end > start:
                    first = start // b
                    last = (end - 1) // b
                    fd = os.open(self._blob, os.O_RDONLY)
                    try:
                        for blk in range(first, last + 1):
                            index = blk + 1  # 1-based
                            pt = self._read_block(fd, index)
                            lo = max(start, blk * b) - blk * b
                            hi = min(end, (blk + 1) * b) - blk * b
                            fout.write(pt[lo:hi])
                            written += hi - lo
                            blocks += 1
                            cipher_bytes += self._desc.block_plain_len(index) + layout.TAG_LEN
                    finally:
                        os.close(fd)
                _fsync_file(fout)
            try:
                os.link(tmp, out)  # atomic, fails if `out` already exists
            except FileExistsError:
                raise TargetExistsError(f"output target already exists: {out}") from None
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return ReadStats(blocks_read=blocks, cipher_bytes_read=cipher_bytes,
                         bytes_written=written)

    def verify(self) -> VerifyStats:
        """Re-authenticate the descriptor and every block, and check the
        whole-archive SHA-256.  Reads the entire blob, O(block_size) memory."""
        ArchiveStore._check_desc_tag(self._aead, self._desc, self._desc_tag)
        self._check_blob_size()
        h = hashlib.sha256()
        total = 0
        fd = os.open(self._blob, os.O_RDONLY)
        try:
            for index in range(layout.FIRST_CONTENT_INDEX,
                               self._desc.block_count + 1):
                pt = self._read_block(fd, index)
                h.update(pt)
                total += len(pt)
        finally:
            os.close(fd)
        if h.digest() != self._desc.sha256:
            raise AuthenticationError(
                f"whole-archive SHA-256 mismatch for {self.archive_id}"
            )
        return VerifyStats(
            blocks_checked=self._desc.block_count,
            bytes_checked=total,
            sha256_hex=h.hexdigest(),
        )
