#!/usr/bin/env python3
"""Reproducible end-to-end drill for warcdb (offline capture fact store).

Story line: three encrypted archives arrive out of order -- first two
levels of revisit references, then the middle reference (root response
still missing), then the root body.  A fetch is watched through
pending -> pending -> resolved.  Afterwards: a newer fetch of the same
URI reusing the old body, an error gallery (cycle, missing target, digest
and length mismatches, Record-ID conflict, truncated archive), parse
read-chunk invariance, SIGKILL before the fact commit, KEK rotation with
export from a new-key-only process, and a large-body memory measurement.

Every storage/fact operation runs in a fresh OS process (demo/worker.py
for archivestore, demo/warc_worker.py for warcdb).  Test keys are
generated at runtime and never committed anywhere.

Run:  python3 demo/run_warc_demo.py
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from warcdb import fixtures  # noqa: E402

WORKER = Path(__file__).resolve().parent / "worker.py"
WARC_WORKER = Path(__file__).resolve().parent / "warc_worker.py"
PY = sys.executable

B = 1024          # archivestore block size for the functional part
BIG_B = 2 << 20   # block size for the large-body archive (2 MiB)

URI1 = "http://example.org/page"
URI2 = "http://example.org/other"
URI3 = "http://example.org/big"

ID_S = "<urn:uuid:00000000-0000-4000-8000-000000000001>"
ID_M = "<urn:uuid:00000000-0000-4000-8000-000000000002>"
ID_T1 = "<urn:uuid:00000000-0000-4000-8000-000000000003>"
ID_T2 = "<urn:uuid:00000000-0000-4000-8000-000000000004>"
ID_T3 = "<urn:uuid:00000000-0000-4000-8000-000000000005>"
ID_T4 = "<urn:uuid:00000000-0000-4000-8000-000000000006>"
ID_X = "<urn:uuid:00000000-0000-4000-8000-000000000010>"
ID_Y = "<urn:uuid:00000000-0000-4000-8000-000000000011>"
ID_P = "<urn:uuid:00000000-0000-4000-8000-000000000012>"
ID_Q = "<urn:uuid:00000000-0000-4000-8000-000000000013>"
ID_Q2 = "<urn:uuid:00000000-0000-4000-8000-000000000017>"
ID_R = "<urn:uuid:00000000-0000-4000-8000-000000000014>"
ID_S2 = "<urn:uuid:00000000-0000-4000-8000-000000000015>"
ID_M2 = "<urn:uuid:00000000-0000-4000-8000-000000000016>"
ID_GHOST = "<urn:uuid:00000000-0000-4000-8000-0000000000ff>"
ID_BIG = "<urn:uuid:00000000-0000-4000-8000-000000000020>"

NOW = "2026-11-14T00:00:00Z"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Demo:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="warcdb-demo-"))
        self.keys_dir = self.root / "keys"          # outside every storage dir
        self.keys_dir.mkdir()
        self.orig_dir = self.root / "originals"
        self.orig_dir.mkdir()
        self.out_dir = self.root / "outputs"
        self.out_dir.mkdir()
        self.storage = self.root / "storage"
        self.facts = self.root / "facts.db"
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

    def _run(self, cmd, expect_error=()) -> dict:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
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
            raise SystemExit(f"worker failed: {' '.join(map(str, cmd))}\n"
                             f"{proc.stdout}\n{proc.stderr}")
        return json.loads(line[len("RESULT "):])

    def store(self, keys: dict, *args: str, expect_error=()) -> dict:
        """Run one archivestore worker process."""
        cmd = [PY, str(WORKER), "--storage", str(self.storage)]
        for kid, path in keys.items():
            cmd += ["--key", f"{kid}={path}"]
        cmd += list(args)
        return self._run(cmd, expect_error)

    def warc(self, facts: Path, *args: str, keys=None, storage=True,
             expect_error=()) -> dict:
        """Run one warcdb worker process."""
        cmd = [PY, str(WARC_WORKER), "--facts", str(facts)]
        if storage:
            cmd += ["--storage", str(self.storage)]
        for kid, path in (keys or {}).items():
            cmd += ["--key", f"{kid}={path}"]
        cmd += list(args)
        return self._run(cmd, expect_error)

    def seal(self, keys: dict, path: Path, block_size: int = B) -> str:
        return self.store(keys, "seal", "--source", str(path),
                          "--key-id", next(iter(keys)),
                          "--block-size", str(block_size))["archive_id"]

    def stats(self, facts: Path = None) -> dict:
        return self.warc(facts or self.facts, "stats", storage=False)

    def expect_counts(self, fetches: int, imports: int, facts: Path = None,
                      **by_status) -> dict:
        st = self.stats(facts)
        assert st["fetches"] == fetches, st
        assert st["imports"] == imports, st
        for status, n in by_status.items():
            assert st["by_status"][status] == n, st
        return st

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        k1 = self.gen_key("k1")
        k2 = self.gen_key("k2")
        self.note(f"runtime-generated KEK files in {self.keys_dir} (mode 0600)")
        keys1 = {"k1": k1}

        # ------------------------------------------------ build the fixtures
        self.step("build WARC fixtures offline (bodies kept as ground truth)")
        body_s = (b"<html>\x00\x00\r\n<head>\r\n\r\n"
                  b"WARC/1.1\r\nWARC-Type: response\r\nContent-Length: 9\r\n"
                  b"\r\nFAKERECORD\r\n\r\n"
                  + bytes(range(256)) * 3 + os.urandom(1500))
        # body_s contains zero bytes, CRLFs and a fake record header on
        # purpose: boundaries must come from lengths, never from scanning.
        expected_body = self.orig_dir / "expected-body-S.bin"
        expected_body.write_bytes(body_s)
        dig_s = fixtures.payload_digest_hex(body_s)

        s_rec = fixtures.response_record(
            ID_S, URI1, "2026-11-10T08:00:00Z", body_s,
            http_headers=[("Content-Type", "text/html"),
                          ("X-Fetch-Marker", "root-response")])
        m_rec = fixtures.revisit_record(
            ID_M, URI1, "2026-11-10T09:00:00Z", ID_S, len(body_s), dig_s,
            http_headers=[("X-Fetch-Marker", "middle-revisit")])
        t1_rec = fixtures.revisit_record(
            ID_T1, URI1, "2026-11-10T10:00:00Z", ID_M, len(body_s), dig_s,
            http_headers=[("X-Fetch-Marker", "revisit-level-1")])
        t2_rec = fixtures.revisit_record(
            ID_T2, URI1, "2026-11-10T11:00:00Z", ID_T1, len(body_s), dig_s,
            http_headers=[("X-Fetch-Marker", "revisit-level-2")])
        # T3/T4: newer fetches of the same URI at the SAME instant, spelled
        # with different timezone offsets (10:00+02:00 == 08:00Z).
        t3_rec = fixtures.revisit_record(
            ID_T3, URI1, "2026-11-12T10:00:00+02:00", ID_S, len(body_s), dig_s,
            http_headers=[("X-Fetch-Marker", "new-fetch-t3")])
        t4_rec = fixtures.revisit_record(
            ID_T4, URI1, "2026-11-12T08:00:00Z", ID_S, len(body_s), dig_s,
            http_headers=[("X-Fetch-Marker", "new-fetch-t4")])

        w1 = fixtures.assemble(t2_rec, t1_rec)   # two reference levels first
        w2 = fixtures.assemble(m_rec)            # middle reference, no root
        w3 = fixtures.assemble(s_rec)            # the root body, last
        w4 = fixtures.assemble(t3_rec, t4_rec)   # newer fetches of same URI
        for name, data in [("w1.warc", w1), ("w2.warc", w2),
                           ("w3.warc", w3), ("w4.warc", w4)]:
            (self.orig_dir / name).write_bytes(data)
        self.note(f"w1={len(w1)}B (T2->T1->?), w2={len(w2)}B (M->S), "
                  f"w3={len(w3)}B (S, body {len(body_s)}B), w4={len(w4)}B "
                  f"(T3,T4 same instant)")

        # ------------------------------------- three archives, out of order
        self.step("archive 1: two revisit levels arrive first (T2->T1->M?)")
        a1 = self.seal(keys1, self.orig_dir / "w1.warc")
        r = self.warc(self.facts, "import", "--archive", a1, keys=keys1)
        assert r["new_fetches"] == 2 and not r["already_imported"]
        self.expect_counts(2, 1, pending=2)
        c = self.warc(self.facts, "chain", "--record-id", ID_T2, storage=False)
        assert c["status"] == "pending" and c["terminal"]["kind"] == "missing"
        assert c["terminal"]["record_id"] == ID_M
        self.note(f"T2 chain: {[h['record_id'][-2:] for h in c['hops']]} "
                  f"-> status={c['status']} ({c['status_detail']})")

        self.step("archive 2: the middle reference arrives, root still missing")
        a2 = self.seal(keys1, self.orig_dir / "w2.warc")
        r = self.warc(self.facts, "import", "--archive", a2, keys=keys1)
        assert r["new_fetches"] == 1
        self.expect_counts(3, 2, pending=3)
        c = self.warc(self.facts, "chain", "--record-id", ID_T2, storage=False)
        assert c["status"] == "pending" and c["terminal"]["record_id"] == ID_S
        assert len(c["hops"]) == 3
        self.note(f"T2 chain: {[h['record_id'][-2:] for h in c['hops']]} "
                  f"-> still {c['status']} ({c['status_detail']})")

        self.step("archive 3: the root response arrives -> T2 resolves")
        a3 = self.seal(keys1, self.orig_dir / "w3.warc")
        r = self.warc(self.facts, "import", "--archive", a3, keys=keys1)
        assert r["new_fetches"] == 1 and r["resolved"] == 4
        self.expect_counts(4, 3, resolved=4)
        c = self.warc(self.facts, "chain", "--record-id", ID_T2, storage=False)
        assert c["status"] == "resolved" and c["endpoint_id"] == ID_S
        assert c["source"] == {"archive_id": a3, "offset": s_rec.body_offset,
                               "length": len(body_s)}
        self.note(f"T2 chain: {[h['record_id'][-2:] for h in c['hops']]} "
                  f"-> resolved; source={a3[:12]}.. "
                  f"[{s_rec.body_offset},{s_rec.body_offset + len(body_s)})")
        self.note(f"source offset matches the fixture's independent layout "
                  f"(body_offset={s_rec.body_offset})")
        out = self.out_dir / "export-T2.bin"
        r = self.warc(self.facts, "export", "--record-id", ID_T2,
                      "--out", str(out), keys=keys1)
        assert out.read_bytes() == expected_body.read_bytes()
        assert r["payload_sha256"] == dig_s
        self.note(f"export T2: {r['bytes_written']}B, sha256 matches the "
                  f"independently kept ground-truth bytes")

        # ------------------------- newer fetch of the same URI, reusing body
        self.step("archive 4: newer fetches of the same URI (tie at one instant)")
        a4 = self.seal(keys1, self.orig_dir / "w4.warc")
        r = self.warc(self.facts, "import", "--archive", a4, keys=keys1)
        assert r["new_fetches"] == 2 and r["resolved"] == 6
        self.expect_counts(6, 4, resolved=6)
        g3 = self.warc(self.facts, "get", "--record-id", ID_T3, storage=False)
        assert g3["date_utc"] == "2026-11-12T08:00:00.000000+00:00"
        self.note("T3's date 2026-11-12T10:00:00+02:00 normalised to "
                  f"{g3['date_utc']} (instants compared in UTC)")
        hit = self.warc(self.facts, "lookup", "--uri", URI1, "--at", NOW,
                        storage=False)
        f = hit["fetch"]
        assert f["record_id"] == ID_T4 and f["status"] == "resolved"
        markers = [h["value"] for h in f["http_headers"]
                   if h["name"] == "X-Fetch-Marker"]
        assert markers == ["new-fetch-t4"], markers
        assert hit["source"] == {"archive_id": a3,
                                 "offset": s_rec.body_offset,
                                 "length": len(body_s)}
        self.note(f"lookup latest -> T4 (tie with T3 at the same instant; "
                  f"lexicographically greatest Record-ID wins)")
        self.note(f"  headers are T4's own (X-Fetch-Marker={markers[0]}), "
                  f"date {f['date_utc']}, http {f['http_status']}")
        self.note(f"  body source is still archive {a3[:12]}.. (the OLD body)")
        out = self.out_dir / "export-T4.bin"
        self.warc(self.facts, "export", "--record-id", ID_T4,
                  "--out", str(out), keys=keys1)
        assert out.read_bytes() == expected_body.read_bytes()
        self.note("  exported bytes == the old root body (new fetch's "
                  "headers, old body)")
        hit = self.warc(self.facts, "lookup", "--uri", URI1,
                        "--at", "2026-11-10T11:00:00Z", storage=False)
        assert hit["fetch"]["record_id"] == ID_T2
        self.note("lookup at 2026-11-10T11:00Z -> T2 (older cutoff, older fetch)")

        # ------------------------------------------------------- error gallery
        self.step("error gallery: cycle / missing target / digest+length "
                  "mismatch / Record-ID conflict / truncated archive")
        # (a) reference cycle X<->Y
        x_rec = fixtures.revisit_record(ID_X, URI2, "2026-11-13T08:00:00Z",
                                        ID_Y, len(body_s), dig_s)
        y_rec = fixtures.revisit_record(ID_Y, URI2, "2026-11-13T09:00:00Z",
                                        ID_X, len(body_s), dig_s)
        w5 = fixtures.assemble(x_rec, y_rec)
        (self.orig_dir / "w5.warc").write_bytes(w5)
        a5 = self.seal(keys1, self.orig_dir / "w5.warc")
        self.warc(self.facts, "import", "--archive", a5, keys=keys1)
        self.expect_counts(8, 5, resolved=6, error=2)
        c = self.warc(self.facts, "chain", "--record-id", ID_X, storage=False)
        assert c["status"] == "error" and "cycle" in c["status_detail"]
        assert c["terminal"]["kind"] == "cycle"
        self.note(f"cycle X<->Y imported; chain(X) status=error: "
                  f"{c['status_detail']}")

        # (b) missing target -> pending
        p_rec = fixtures.revisit_record(ID_P, URI2, "2026-11-13T10:00:00Z",
                                        ID_GHOST, len(body_s), dig_s)
        w6 = fixtures.assemble(p_rec)
        (self.orig_dir / "w6.warc").write_bytes(w6)
        a6 = self.seal(keys1, self.orig_dir / "w6.warc")
        self.warc(self.facts, "import", "--archive", a6, keys=keys1)
        self.expect_counts(9, 6, resolved=6, error=2, pending=1)
        c = self.warc(self.facts, "chain", "--record-id", ID_P, storage=False)
        assert c["status"] == "pending" and c["terminal"]["record_id"] == ID_GHOST
        self.note(f"missing target: chain(P) pending ({c['status_detail']})")

        # (c) revisit vs endpoint: digest mismatch and length mismatch
        q_rec = fixtures.revisit_record(ID_Q, URI1, "2026-11-13T11:00:00Z",
                                        ID_S, len(body_s),
                                        fixtures.payload_digest_hex(b"WRONG"))
        q2_rec = fixtures.revisit_record(ID_Q2, URI1, "2026-11-13T11:30:00Z",
                                         ID_S, len(body_s) + 1, dig_s)
        w7 = fixtures.assemble(q_rec, q2_rec)
        (self.orig_dir / "w7.warc").write_bytes(w7)
        a7 = self.seal(keys1, self.orig_dir / "w7.warc")
        self.warc(self.facts, "import", "--archive", a7, keys=keys1)
        self.expect_counts(11, 7, resolved=6, error=4, pending=1)
        c = self.warc(self.facts, "chain", "--record-id", ID_Q, storage=False)
        assert c["status"] == "error" and "digest mismatch" in c["status_detail"]
        self.note(f"digest mismatch: chain(Q) error: {c['status_detail'][:72]}...")
        c2 = self.warc(self.facts, "chain", "--record-id", ID_Q2, storage=False)
        assert c2["status"] == "error" and "length" in c2["status_detail"]
        self.note(f"length mismatch: chain(Q2) error: {c2['status_detail'][:72]}...")
        hit = self.warc(self.facts, "lookup", "--uri", URI1, "--at", NOW,
                        storage=False)
        assert hit["fetch"]["record_id"] == ID_Q2 and hit["fetch"]["status"] == "error"
        assert hit["source"] is None
        self.note("lookup latest of URI1 -> Q2 with status=error, reported "
                  "as-is (no silent fallback to an older fetch)")
        hit = self.warc(self.facts, "lookup", "--uri", URI1,
                        "--at", "2026-11-13T11:15:00Z", storage=False)
        assert hit["fetch"]["record_id"] == ID_Q and hit["fetch"]["status"] == "error"
        hit = self.warc(self.facts, "lookup", "--uri", URI1,
                        "--at", "2026-11-12T09:00:00Z", storage=False)
        assert hit["fetch"]["record_id"] == ID_T4
        self.note("cutoffs before the broken fetches still reach T4 / Q")

        # (d) response record whose WARC-Payload-Digest lies: import rejected
        r_rec = fixtures.response_record(
            ID_R, URI2, "2026-11-13T12:00:00Z", b"some body",
            payload_digest=fixtures.payload_digest_hex(b"different body"))
        w8 = fixtures.assemble(r_rec)
        (self.orig_dir / "w8.warc").write_bytes(w8)
        a8 = self.seal(keys1, self.orig_dir / "w8.warc")
        err = self.warc(self.facts, "import", "--archive", a8, keys=keys1,
                        expect_error=("DigestMismatchError",))
        self.note(f"bad payload digest in a response: import rejected "
                  f"({err['type']})")
        self.expect_counts(11, 7, resolved=6, error=4, pending=1)

        # (e) same Record-ID, different bytes: conflict, old row untouched
        t2_alt = fixtures.revisit_record(ID_T2, URI1, "2026-11-10T11:00:01Z",
                                         ID_T1, len(body_s), dig_s)
        w9 = fixtures.assemble(t2_alt)
        (self.orig_dir / "w9.warc").write_bytes(w9)
        a9 = self.seal(keys1, self.orig_dir / "w9.warc")
        err = self.warc(self.facts, "import", "--archive", a9, keys=keys1,
                        expect_error=("ImportConflictError",))
        self.note(f"same Record-ID with different bytes: import rejected "
                  f"({err['type']})")
        g = self.warc(self.facts, "get", "--record-id", ID_T2, storage=False)
        assert g["date_utc"] == "2026-11-10T11:00:00.000000+00:00"
        self.expect_counts(11, 7, resolved=6, error=4, pending=1)
        self.note("stored T2 row is unchanged (date still 11:00:00Z)")

        # (f) multi-record archive whose LAST record is truncated
        s2_rec = fixtures.response_record(ID_S2, URI2, "2026-11-13T13:00:00Z",
                                          b"second root body")
        m2_rec = fixtures.revisit_record(ID_M2, URI2, "2026-11-13T13:01:00Z",
                                         ID_S2, len(b"second root body"),
                                         fixtures.payload_digest_hex(
                                             b"second root body"))
        w10 = fixtures.assemble(s2_rec, m2_rec)[:-100]   # cut the last record
        (self.orig_dir / "w10.warc").write_bytes(w10)
        a10 = self.seal(keys1, self.orig_dir / "w10.warc")
        err = self.warc(self.facts, "import", "--archive", a10, keys=keys1,
                        expect_error=("WarcTruncatedError",))
        self.note(f"truncated last record: whole import rejected ({err['type']})")
        self.expect_counts(11, 7, resolved=6, error=4, pending=1)
        self.note("the two good records of that archive left NO rows behind "
                  "(fetches still 11, imports still 7)")

        # --------------------------------------- parse read-chunk invariance
        self.step("tiny vs large parse read chunk give identical facts")
        facts_tiny = self.root / "facts-tiny.db"
        facts_big = self.root / "facts-big.db"
        for aid in (a1, a2, a3, a4, a5, a6, a7):
            self.warc(facts_tiny, "import", "--archive", aid,
                      "--read-chunk", "7", keys=keys1)
            self.warc(facts_big, "import", "--archive", aid,
                      "--read-chunk", "65536", keys=keys1)
        dump_tiny = self.warc(facts_tiny, "dump", storage=False)
        dump_big = self.warc(facts_big, "dump", storage=False)
        dump_main = self.warc(self.facts, "dump", storage=False)
        for d in (dump_tiny, dump_big, dump_main):
            d.pop("mem", None)
        assert dump_tiny == dump_big == dump_main
        self.note("read_chunk=7 (header terminator and Content-Length span "
                  "read segments) == read_chunk=65536 == main DB, "
                  f"{len(dump_main['fetches'])} fetches compared byte-for-byte")

        # --------------------------------------- SIGKILL before fact commit
        self.step("SIGKILL before the fact commit: no half batch, clean re-import")
        facts_kill = self.root / "facts-kill.db"
        pause = self.root / "pause-kill"
        pause.mkdir()
        proc = subprocess.Popen(
            [PY, str(WARC_WORKER), "--facts", str(facts_kill),
             "--storage", str(self.storage), "--key", f"k1={k1}",
             "import", "--archive", a1,
             "--pause-stage", "pre_commit", "--pause-dir", str(pause)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 60
        while not (pause / "pre_commit.paused").exists():
            assert time.monotonic() < deadline and proc.poll() is None
            time.sleep(0.05)
        st = self.warc(facts_kill, "stats", storage=False)
        assert st["fetches"] == 0 and st["imports"] == 0
        self.note("while the import transaction is paused pre-commit, other "
                  "processes see 0 fetches")
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=30)
        assert proc.returncode == -signal.SIGKILL
        st = self.stats(facts_kill)
        assert st["fetches"] == 0 and st["imports"] == 0
        self.note("after SIGKILL: 0 fetches, 0 imports -- no half batch")
        r = self.warc(facts_kill, "import", "--archive", a1, keys=keys1)
        assert r["new_fetches"] == 2
        self.expect_counts(2, 1, facts=facts_kill, pending=2)
        self.note("restarted import succeeds: 2 fetches")
        # the same original sealed again is a different archive_id with the
        # same records: duplicates, fetch count does not grow
        a1b = self.seal(keys1, self.orig_dir / "w1.warc")
        assert a1b != a1
        r = self.warc(facts_kill, "import", "--archive", a1b, keys=keys1)
        assert r["duplicates"] == 2 and r["new_fetches"] == 0
        self.expect_counts(2, 2, facts=facts_kill, pending=2)
        self.note("same records under a new archive_id: 2 duplicates, fetch "
                  "count unchanged")
        r = self.warc(facts_kill, "import", "--archive", a1, keys=keys1)
        assert r["already_imported"] and r["new_fetches"] == 0
        self.note("re-importing the first archive itself: idempotent "
                  "confirmation, still 2 fetches")

        # -------------------------------------- rotate KEK, export with k2 only
        self.step("rotate KEK k1 -> k2; new-key-only process still queries/exports")
        r = self.store({"k1": k1, "k2": k2}, "rotate", "--old", "k1", "--new", "k2")
        self.note(f"rotated {r['rotated']} envelopes in one transaction")
        out = self.out_dir / "export-T4-post-rotate.bin"
        self.warc(self.facts, "export", "--record-id", ID_T4,
                  "--out", str(out), keys={"k2": k2})
        assert out.read_bytes() == expected_body.read_bytes()
        self.note("process holding ONLY k2 exported T4's body; bytes match "
                  "the pre-rotation ground truth")
        self.warc(self.facts, "export", "--record-id", ID_T4,
                  "--out", str(self.out_dir / "export-oldkey.bin"),
                  keys={"k1": k1}, expect_error=("KeyUnavailableError",))
        assert not (self.out_dir / "export-oldkey.bin").exists()
        self.note("old-key-only process is refused and leaves no file")
        facts_bytes = self.facts.read_bytes()
        assert k1.read_bytes() not in facts_bytes
        assert k2.read_bytes() not in facts_bytes
        self.note("facts.db contains neither KEK (metadata and byte ranges "
                  "only, no key material)")

        # ------------------------------------------------------- large body
        self.step("large body: input size vs measured peak memory")
        big_len = 256 * 1024 * 1024
        big_body = self.orig_dir / "big-body.bin"
        with open(big_body, "wb") as f:
            for _ in range(256):
                f.write(os.urandom(1 << 20))
        big_dig = sha256_file(big_body)
        big_warc = self.orig_dir / "big.warc"
        big_rec = fixtures.response_record_stream(
            ID_BIG, URI3, "2026-11-13T14:00:00Z", big_body, big_warc)
        big_size = big_warc.stat().st_size
        self.note(f"input: one response record, body {big_len >> 20} MiB, "
                  f"WARC {big_size >> 20} MiB")
        r = self.store(keys1, "seal", "--source", str(big_warc),
                       "--key-id", "k1", "--block-size", str(BIG_B))
        a_big = r["archive_id"]
        self.note(f"seal:   peak RSS {r['mem']['max_rss_kb'] / 1024:.0f} MiB "
                  f"(block size 2 MiB)")
        r = self.warc(self.facts, "import", "--archive", a_big,
                      "--read-chunk", str(1 << 20), keys=keys1)
        rss_import = r["mem"]["max_rss_kb"]
        self.note(f"import: peak RSS {rss_import / 1024:.0f} MiB for a "
                  f"{big_size >> 20} MiB archive (streaming decrypt + digests)")
        assert rss_import < big_len // 1024 // 2, "import RSS must stay far below input size"
        g = self.warc(self.facts, "get", "--record-id", ID_BIG, storage=False)
        assert g["status"] == "resolved"
        assert g["body_offset"] == big_rec.body_offset
        assert g["body_length"] == big_len
        out = self.out_dir / "export-big.bin"
        r = self.warc(self.facts, "export", "--record-id", ID_BIG,
                      "--out", str(out), keys=keys1)
        rss_export = r["mem"]["max_rss_kb"]
        assert sha256_file(out) == big_dig
        self.note(f"export: peak RSS {rss_export / 1024:.0f} MiB; exported "
                  f"{r['bytes_written'] >> 20} MiB, sha256 == ground truth")
        assert rss_export < big_len // 1024 // 2
        self.note(f"=> 256 MiB body imported/exported with peak RSS "
                  f"~{max(rss_import, rss_export) / 1024:.0f} MiB "
                  f"(dominated by block size, not archive size)")

        print(f"\nDEMO OK -- all checks passed.  workspace kept at {self.root}")


if __name__ == "__main__":
    Demo().run()
