#!/usr/bin/env python3
"""Reproducible end-to-end drill for archivestore.

Every storage operation runs in a fresh OS process (demo/worker.py), so
"process" claims in the spec are exercised for real, including SIGKILL at
controlled points of the rotation transaction.  Test keys are generated at
runtime and never committed anywhere.

Run:  python3 demo/run_demo.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from archivestore import layout  # noqa: E402  (offsets for tampering only)

WORKER = Path(__file__).resolve().parent / "worker.py"
REFDEC = Path(__file__).resolve().parent / "refdec.py"
PY = sys.executable

B = 1024          # block size for the functional part of the drill
TAG = layout.TAG_LEN
STORED = B + TAG  # stored bytes per full block


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Demo:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="archivestore-demo-"))
        self.keys_dir = self.root / "keys"          # outside every storage dir
        self.keys_dir.mkdir()
        self.orig_dir = self.root / "originals"
        self.orig_dir.mkdir()
        self.out_dir = self.root / "outputs"
        self.out_dir.mkdir()
        self.n = 0
        print(f"demo workspace: {self.root}")

    # ------------------------------------------------------------- helpers
    def step(self, title: str) -> None:
        self.n += 1
        print(f"\n=== step {self.n}: {title}")

    def note(self, msg: str) -> None:
        print(f"    {msg}")

    def gen_key(self, key_id: str) -> Path:
        from archivestore.keys import generate_key_file

        path = self.keys_dir / f"{key_id}.key"
        generate_key_file(path)
        return path

    def new_storage(self, name: str) -> Path:
        return self.root / name

    def worker(self, storage: Path, keys: dict, *args: str,
               expect_error: tuple[str, ...] = ()) -> dict:
        """Run one worker process.  With expect_error, a matching ERROR line
        and non-zero exit are required; otherwise success is required."""
        cmd = [PY, str(WORKER), "--storage", str(storage)]
        for kid, path in keys.items():
            cmd += ["--key", f"{kid}={path}"]
        cmd += list(args)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        if expect_error:
            if proc.returncode == 0 or not line.startswith("ERROR "):
                raise SystemExit(
                    f"expected error {expect_error} but got rc={proc.returncode}: "
                    f"{proc.stdout} {proc.stderr}")
            err = json.loads(line[len("ERROR "):])
            if err["type"] not in expect_error:
                raise SystemExit(f"expected error {expect_error}, got {err}")
            return err
        if proc.returncode != 0 or not line.startswith("RESULT "):
            raise SystemExit(f"worker failed: {' '.join(cmd)}\n{proc.stdout}\n{proc.stderr}")
        return json.loads(line[len("RESULT "):])

    def make_original(self, name: str, parts: list) -> Path:
        path = self.orig_dir / name
        with open(path, "wb") as f:
            for part in parts:
                f.write(part)
        return path

    # --------------------------------------------------------------- steps
    def run(self) -> None:
        k1 = self.gen_key("k1")
        k2 = self.gen_key("k2")
        k_wrong = self.gen_key("k_wrong")
        self.note(f"runtime-generated KEK files in {self.keys_dir} (mode 0600)")

        # Two originals containing zero-byte runs, one empty original.
        o1 = self.make_original("o1.bin", [os.urandom(1200), b"\0" * 800,
                                           os.urandom(1500), b"\0" * 1500])  # 5000
        o2 = self.make_original("o2.bin", [os.urandom(1000), b"\0" * 600,
                                           os.urandom(1000)])                 # 2600
        o3 = self.make_original("o3.bin", [])                                  # 0
        o1_bytes, o2_bytes = o1.read_bytes(), o2.read_bytes()

        # ---------------------------------------------------------------- seal
        self.step("seal three originals (fresh process each, B=1024, KEK k1)")
        st_a = self.new_storage("storageA")
        a1 = self.worker(st_a, {"k1": k1}, "seal", "--source", str(o1),
                         "--key-id", "k1", "--block-size", str(B))["archive_id"]
        a2 = self.worker(st_a, {"k1": k1}, "seal", "--source", str(o2),
                         "--key-id", "k1", "--block-size", str(B))["archive_id"]
        a3 = self.worker(st_a, {"k1": k1}, "seal", "--source", str(o3),
                         "--key-id", "k1", "--block-size", str(B))["archive_id"]
        self.note(f"archives: o1={a1[:12]}.. o2={a2[:12]}.. o3(empty)={a3[:12]}..")

        # The catalogue and blobs must not contain plaintext.
        db_bytes = (st_a / "archive.db").read_bytes()
        blob1 = (st_a / "blobs" / f"{a1}.bin").read_bytes()
        probe = o1_bytes[:64]
        assert probe not in db_bytes and probe not in blob1
        self.note("plaintext probe of first 64 source bytes: absent from "
                  "archive.db and blob (only ciphertext/tags/envelopes stored)")

        # ------------------------------------------------------- ranged reads
        self.step("read ranges in NEW processes and compare with original slices")
        cases = [
            ("first 300 bytes of o1", a1, 0, 300, o1_bytes),
            ("cross-block 900..2100 of o1 (3 blocks)", a1, 900, 2100, o1_bytes),
            ("tail 4900..5000 of o1 (short last block)", a1, 4900, 5000, o1_bytes),
            ("cross-block 1000..2200 of o2", a2, 1000, 2200, o2_bytes),
            ("zero-length range 100..100 of o1", a1, 100, 100, o1_bytes),
            ("empty archive o3, range 0..0", a3, 0, 0, b""),
        ]
        for label, aid, s, e, original in cases:
            out = self.out_dir / f"read-{self.n}-{s}-{e}.bin"
            r = self.worker(st_a, {"k1": k1}, "read", "--archive", aid,
                            f"--start={s}", f"--end={e}", "--out", str(out))
            got = out.read_bytes()
            assert got == original[s:e], label
            self.note(f"OK  {label}: {r['bytes_written']}B via "
                      f"{r['blocks_read']} block(s), {r['cipher_bytes_read']}B cipher I/O")

        # Illegal ranges are rejected.
        for s, e in [(-1, 5), (5, 2), (0, 5001), (0, 1)]:
            aid = a1 if (s, e) != (0, 1) else a3  # [0,1) illegal on empty archive
            err = self.worker(st_a, {"k1": k1}, "read", "--archive", aid,
                              f"--start={s}", f"--end={e}",
                              "--out", str(self.out_dir / "bad.bin"),
                              expect_error=("RangeError",))
            self.note(f"OK  range [{s},{e}) rejected: {err['type']}")

        # A failed read must not clobber an existing target.
        victim = self.out_dir / "victim.bin"
        victim.write_bytes(b"keep me")
        self.worker(st_a, {"k1": k1}, "read", "--archive", a1,
                    "--start=0", "--end=10", "--out", str(victim),
                    expect_error=("TargetExistsError",))
        assert victim.read_bytes() == b"keep me"
        self.note("OK  existing target untouched (read refuses to overwrite)")

        # ------------------------------------------------- reference decryptor
        self.step("independent reference decryptor (no archivestore code)")
        for aid, original, name in [(a1, o1_bytes, "o1"), (a3, b"", "o3-empty")]:
            out = self.out_dir / f"refdec-{name}.bin"
            proc = subprocess.run(
                [PY, str(REFDEC), "--storage", str(st_a), "--archive", aid,
                 "--key-file", str(k1), "--out", str(out)],
                capture_output=True, text=True, timeout=120)
            assert proc.returncode == 0, proc.stderr
            info = json.loads(proc.stdout.strip())
            assert out.read_bytes() == original
            self.note(f"OK  refdec {name}: {info['block_count']} blocks, "
                      f"sha256={info['sha256'][:16]}.. matches original")

        # ------------------------------------------------------------- tamper
        self.step("tamper cases: each must fail (on disposable copies)")
        blob_rel = lambda aid: Path("blobs") / f"{aid}.bin"  # noqa: E731

        def fresh_copy(name: str) -> Path:
            dst = self.root / name
            shutil.copytree(st_a, dst)
            return dst

        def mutate_blob(storage: Path, aid: str, fn) -> None:
            p = storage / blob_rel(aid)
            data = bytearray(p.read_bytes())
            fn(data)
            p.write_bytes(bytes(data))

        # 1. swap two blocks inside one archive
        st = fresh_copy("t-swap")
        mutate_blob(st, a1, lambda d: d.__setitem__(
            slice(STORED, 3 * STORED), d[2 * STORED:3 * STORED] + d[STORED:2 * STORED]))
        self.worker(st, {"k1": k1}, "verify", "--archive", a1,
                    expect_error=("AuthenticationError",))
        self.note("OK  swapped blocks within archive -> verify fails")

        # 2. splice in a block from another archive
        st = fresh_copy("t-foreign")
        foreign = (st_a / blob_rel(a2)).read_bytes()[STORED:2 * STORED]
        mutate_blob(st, a1, lambda d: d.__setitem__(slice(STORED, 2 * STORED), foreign))
        self.worker(st, {"k1": k1}, "verify", "--archive", a1,
                    expect_error=("AuthenticationError",))
        self.note("OK  block imported from another archive -> verify fails")

        # 3. modify the declared length in the descriptor
        st = fresh_copy("t-desclen")
        con = sqlite3.connect(st / "archive.db")
        raw = bytearray(con.execute("SELECT descriptor FROM archives WHERE archive_id=?",
                                    (bytes.fromhex(a1),)).fetchone()[0])
        raw[29] ^= 0x01  # low byte of the 8-byte orig_len field
        con.execute("UPDATE archives SET descriptor=? WHERE archive_id=?",
                    (bytes(raw), bytes.fromhex(a1)))
        con.commit(); con.close()
        self.worker(st, {"k1": k1}, "verify", "--archive", a1,
                    expect_error=("AuthenticationError", "DescriptorError"))
        self.note("OK  descriptor length field modified -> open/verify fails")

        # 4. truncate the blob
        st = fresh_copy("t-truncate")
        p = st / blob_rel(a1)
        p.write_bytes(p.read_bytes()[:-100])
        self.worker(st, {"k1": k1}, "verify", "--archive", a1,
                    expect_error=("BlobStateError",))
        self.note("OK  truncated ciphertext -> verify fails")

        # 5. append extra ciphertext
        st = fresh_copy("t-append")
        p = st / blob_rel(a1)
        p.write_bytes(p.read_bytes() + os.urandom(64))
        self.worker(st, {"k1": k1}, "verify", "--archive", a1,
                    expect_error=("BlobStateError",))
        self.note("OK  appended extra ciphertext -> verify fails")

        # 6. wrong KEK
        self.worker(st_a, {"k1": k_wrong}, "verify", "--archive", a1,
                    expect_error=("AuthenticationError",))
        self.note("OK  wrong KEK -> DEK unwrap fails")

        # 7. storage + catalogue copied out without any key
        st = fresh_copy("t-leaked")
        self.worker(st, {}, "read", "--archive", a1, "--start=0", "--end=10",
                    "--out", str(self.out_dir / "leaked.bin"),
                    expect_error=("KeyUnavailableError",))
        self.note("OK  copied storage without keys -> unreadable")

        # 8. corrupt an UNACCESSED block: local read still works, verify fails
        st = fresh_copy("t-partial")
        last_block_off = 4 * STORED  # o1: 5 blocks, corrupt block 5
        mutate_blob(st, a1, lambda d: d.__setitem__(last_block_off + 10,
                                                    d[last_block_off + 10] ^ 0xFF))
        out = self.out_dir / "partial-ok.bin"
        r = self.worker(st, {"k1": k1}, "read", "--archive", a1,
                        "--start=0", "--end=300", "--out", str(out))
        assert out.read_bytes() == o1_bytes[0:300]
        self.note(f"OK  unaccessed block corrupted: read[0,300) still works "
                  f"({r['blocks_read']} block touched)")
        self.worker(st, {"k1": k1}, "read", "--archive", a1,
                    "--start=4000", "--end=5000",
                    "--out", str(self.out_dir / "partial-bad.bin"),
                    expect_error=("AuthenticationError",))
        self.note("OK  ...but reading the corrupted region fails")
        self.worker(st, {"k1": k1}, "verify", "--archive", a1,
                    expect_error=("AuthenticationError",))
        self.note("OK  ...and full verify reports it")

        # 9. rotate must reject the whole round if one envelope is corrupt
        st = fresh_copy("t-rotate-corrupt")
        con = sqlite3.connect(st / "archive.db")
        w = bytearray(con.execute("SELECT wrapped_dek FROM envelopes WHERE archive_id=?",
                                  (bytes.fromhex(a2),)).fetchone()[0])
        w[20] ^= 0x01
        con.execute("UPDATE envelopes SET wrapped_dek=? WHERE archive_id=?",
                    (bytes(w), bytes.fromhex(a2)))
        con.commit(); con.close()
        self.worker(st, {"k1": k1, "k2": k2}, "rotate", "--old", "k1", "--new", "k2",
                    expect_error=("AuthenticationError", "RotationError"))
        r = self.worker(st, {"k1": k1}, "verify", "--archive", a1)
        self.note(f"OK  corrupt envelope -> rotation rejected, catalogue still "
                  f"all-old (verify with old key OK, {r['blocks_checked']} blocks)")

        # ------------------------------------------------------------ rotation
        self.step("rotate k1 -> k2: ciphertext and descriptors untouched")
        def digest_state(storage: Path):
            con = sqlite3.connect(storage / "archive.db")
            rows = con.execute(
                "SELECT hex(a.archive_id), a.descriptor, e.key_id, e.wrapped_dek"
                " FROM archives a JOIN envelopes e USING(archive_id)").fetchall()
            con.close()
            blobs = {p.name: sha256_file(p) for p in (storage / "blobs").glob("*.bin")}
            descs = {r[0]: hashlib.sha256(r[1]).hexdigest() for r in rows}
            envs = {r[0]: (r[2], hashlib.sha256(r[3]).hexdigest()) for r in rows}
            return blobs, descs, envs

        blobs0, descs0, envs0 = digest_state(st_a)
        r = self.worker(st_a, {"k1": k1, "k2": k2}, "rotate", "--old", "k1",
                        "--new", "k2")
        self.note(f"rotated {r['rotated']} envelopes in one transaction")
        blobs1, descs1, envs1 = digest_state(st_a)
        assert blobs0 == blobs1, "blob bytes changed!"
        assert descs0 == descs1, "descriptor bytes changed!"
        assert all(envs0[a][1] != envs1[a][1] for a in envs0)
        assert all(envs1[a][0] == "k2" for a in envs1)
        self.note("OK  blob SHA-256 set identical, descriptor SHA-256 set identical;")
        self.note("OK  only envelopes changed (new random nonces, key_id=k2)")

        self.worker(st_a, {"k1": k1}, "verify", "--archive", a1,
                    expect_error=("KeyUnavailableError",))
        self.note("OK  old KEK alone can no longer open the current catalogue")
        for aid, original in [(a1, o1_bytes), (a2, o2_bytes), (a3, b"")]:
            self.worker(st_a, {"k2": k2}, "verify", "--archive", aid)
        out = self.out_dir / "post-rotate-tail.bin"
        self.worker(st_a, {"k2": k2}, "read", "--archive", a1,
                    "--start=4900", "--end=5000", "--out", str(out))
        assert out.read_bytes() == o1_bytes[4900:5000]
        self.note("OK  new KEK alone verifies all archives and reads back the tail")

        # ------------------------------------------- kill on both commit sides
        self.step("SIGKILL on both sides of the rotation commit")
        for stage, expect_old_after in [("pre_commit", True), ("post_commit", False)]:
            st = self.new_storage(f"storage-kill-{stage}")
            x1 = self.make_original(f"kx1-{stage}.bin", [os.urandom(1500)])
            x2 = self.make_original(f"kx2-{stage}.bin", [os.urandom(700)])
            xa = self.worker(st, {"k1": k1}, "seal", "--source", str(x1),
                             "--key-id", "k1", "--block-size", str(B))["archive_id"]
            xb = self.worker(st, {"k1": k1}, "seal", "--source", str(x2),
                             "--key-id", "k1", "--block-size", str(B))["archive_id"]
            pause = self.root / f"pause-{stage}"
            pause.mkdir()
            proc = subprocess.Popen(
                [PY, str(WORKER), "--storage", str(st),
                 "--key", f"k1={k1}", "--key", f"k2={k2}",
                 "rotate", "--old", "k1", "--new", "k2",
                 "--pause-stage", stage, "--pause-dir", str(pause)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            deadline = time.monotonic() + 60
            while not (pause / f"{stage}.paused").exists():
                assert time.monotonic() < deadline and proc.poll() is None
                time.sleep(0.05)
            if stage == "pre_commit":
                # While the transaction is uncommitted, other processes still
                # see the complete OLD generation.
                out = self.out_dir / "during-pause.bin"
                self.worker(st, {"k1": k1}, "read", "--archive", xa,
                            "--start=0", "--end=64", "--out", str(out))
                assert out.read_bytes() == x1.read_bytes()[:64]
                self.note("OK  during pre-commit pause: readers see all-old")
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=30)
            assert proc.returncode == -signal.SIGKILL
            good, bad = (k1, k2) if expect_old_after else (k2, k1)
            good_id, bad_id = ("k1", "k2") if expect_old_after else ("k2", "k1")
            self.worker(st, {good_id: good}, "verify", "--archive", xa)
            self.worker(st, {good_id: good}, "verify", "--archive", xb)
            self.worker(st, {bad_id: bad}, "verify", "--archive", xa,
                        expect_error=("KeyUnavailableError",))
            con = sqlite3.connect(st / "archive.db")
            kids = {r[0] for r in con.execute("SELECT key_id FROM envelopes")}
            con.close()
            assert kids == {good_id}, kids
            self.note(f"OK  kill at {stage}: reopen sees a complete "
                      f"{'OLD' if expect_old_after else 'NEW'} generation only")

        # --------------------------------- reader holding DEK across rotation
        self.step("a reader that already unwrapped keeps reading across rotation")
        st = self.new_storage("storage-hold")
        hx = self.make_original("hx.bin", [os.urandom(2000), b"\0" * 500, os.urandom(800)])
        hx_bytes = hx.read_bytes()
        ha = self.worker(st, {"k1": k1}, "seal", "--source", str(hx),
                         "--key-id", "k1", "--block-size", str(B))["archive_id"]
        sentinel = self.root / "hold.open"
        go = self.root / "hold.go"
        o1p, o2p = self.out_dir / "hold-1.bin", self.out_dir / "hold-2.bin"
        proc = subprocess.Popen(
            [PY, str(WORKER), "--storage", str(st), "--key", f"k1={k1}",
             "hold-read", "--archive", ha,
             "--sentinel", str(sentinel), "--go", str(go),
             "--range", f"0:300:{o1p}", "--range", f"3000:3300:{o2p}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 60
        while not sentinel.exists():
            assert time.monotonic() < deadline and proc.poll() is None
            time.sleep(0.05)
        self.note("reader opened handle and unwrapped DEK with k1; rotating now")
        self.worker(st, {"k1": k1, "k2": k2}, "rotate", "--old", "k1", "--new", "k2")
        go.write_text("go")
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
        assert o1p.read_bytes() == hx_bytes[0:300]
        assert o2p.read_bytes() == hx_bytes[3000:3300]
        self.note("OK  pre-rotation reader finished its ranges with the held DEK")
        self.worker(st, {"k1": k1}, "read", "--archive", ha, "--start=0",
                    "--end=10", "--out", str(self.out_dir / "hold-new.bin"),
                    expect_error=("KeyUnavailableError",))
        self.note("OK  NEW opens with the old key are refused after rotation")

        # ------------------------------------------------- memory and real I/O
        self.step("large original: peak memory and narrow-range I/O vs block size")
        big_len = 64 * 1024 * 1024
        big = self.orig_dir / "big.bin"
        with open(big, "wb") as f:
            for _ in range(64):
                f.write(os.urandom(1024 * 1024))
        st_m = self.new_storage("storageM")
        t0 = time.monotonic()
        r1 = self.worker(st_m, {"k1": k1}, "seal", "--source", str(big),
                         "--key-id", "k1", "--block-size", str(1 << 20))
        t1 = time.monotonic()
        big_a = r1["archive_id"]
        rss1 = r1["mem"]["max_rss_kb"]
        self.note(f"seal 64MiB, B=1MiB: peak RSS {rss1/1024:.0f} MiB "
                  f"({t1-t0:.1f}s) -- archive is {big_len >> 20} MiB")
        assert rss1 < big_len // 1024, "peak RSS must stay far below archive size"

        st_m2 = self.new_storage("storageM2")
        r8 = self.worker(st_m2, {"k1": k1}, "seal", "--source", str(big),
                         "--key-id", "k1", "--block-size", str(8 << 20))
        rss8 = r8["mem"]["max_rss_kb"]
        self.note(f"seal 64MiB, B=8MiB: peak RSS {rss8/1024:.0f} MiB "
                  f"(grows with B, not with archive length)")
        assert rss8 > rss1 + 4 * 1024

        out = self.out_dir / "big-narrow.bin"
        rn = self.worker(st_m, {"k1": k1}, "read", "--archive", big_a,
                         "--start=30000000", "--end=30001000", "--out", str(out))
        self.note(f"narrow read 1000B @30MB: touched {rn['blocks_read']} block, "
                  f"{rn['cipher_bytes_read']}B cipher I/O (B+16), "
                  f"peak RSS {rn['mem']['max_rss_kb']/1024:.0f} MiB")
        assert rn["blocks_read"] == 1 and rn["cipher_bytes_read"] == (1 << 20) + TAG
        with open(big, "rb") as f:
            f.seek(30000000)
            assert out.read_bytes() == f.read(1000)

        s, e = 29 * (1 << 20) - 1000, 29 * (1 << 20) + 1000
        out2 = self.out_dir / "big-cross.bin"
        rc = self.worker(st_m, {"k1": k1}, "read", "--archive", big_a,
                         f"--start={s}", f"--end={e}", "--out", str(out2))
        assert rc["blocks_read"] == 2
        self.note(f"cross-boundary read 2000B: {rc['blocks_read']} blocks, "
                  f"{rc['cipher_bytes_read']}B cipher I/O")
        rv = self.worker(st_m, {"k1": k1}, "verify", "--archive", big_a)
        self.note(f"verify 64MiB: {rv['blocks_checked']} blocks, "
                  f"peak RSS {rv['mem']['max_rss_kb']/1024:.0f} MiB")
        self.note("=> memory and narrow-range I/O are governed by block "
                  "length B, not by total archive length")

        print(f"\nDEMO OK -- all checks passed.  workspace kept at {self.root}")


if __name__ == "__main__":
    Demo().run()
