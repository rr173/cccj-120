"""Regression tests for eventledger (stdlib unittest, no network).

Run:
    python3 -m unittest tests.test_eventledger -v
or:
    python3 tests/test_eventledger.py
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eventledger import (  # noqa: E402
    EventConflictError,
    Ledger,
    LedgerNotFoundError,
    ValidationError,
    parse_event_time,
)
from eventledger.statemachine import (  # noqa: E402
    OUTCOME_CANCELLED,
    OUTCOME_FAILED,
    OUTCOME_QUEUED,
    OUTCOME_RUNNING,
    OUTCOME_SUCCESS,
    OUTCOME_WAITING_RETRY,
)


def ev(run, req, attempt, seq, etype, etime, **extra):
    base = {
        "run_id": run, "request_id": req, "attempt": attempt, "seq": seq,
        "event_id": f"{run}-{req}-{seq}", "event_type": etype,
        "event_time": etime,
    }
    base.update(extra)
    return base


def write_jsonl(path, events, raw_lines=None):
    with open(path, "w", encoding="utf-8") as fh:
        if raw_lines is not None:
            for line in raw_lines:
                fh.write(line + "\n")
            return
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


class EventLedgerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "ledger.db"

    def ledger(self):
        return Ledger(self.db)

    def path(self, name):
        return self.dir / name


class StructuralTests(EventLedgerTestBase):
    def test_bad_json_and_truncated_last_line(self):
        p = self.path("bad.jsonl")
        good = ev("r", "q", 1, 1, "queued", "2026-11-14T00:00:00Z")
        write_jsonl(p, [good], raw_lines=[
            json.dumps(good),
            '{"run_id": "r", "request_id": "q", "attempt": 1, "seq": 2',
        ])
        with self.ledger() as l:
            with self.assertRaises(ValidationError) as cm:
                l.import_file(p)
        self.assertEqual(cm.exception.problems[0].line, 2)
        with self.ledger() as l:
            self.assertEqual(l.event_count(), 0)  # whole batch rolled back

    def test_naive_timestamp_rejected(self):
        p = self.path("naive.jsonl")
        write_jsonl(p, [ev("r", "q", 1, 1, "queued",
                           "2026-11-14T00:00:00")])
        with self.ledger() as l:
            with self.assertRaises(ValidationError) as cm:
                l.import_file(p)
        self.assertIn("timezone", cm.exception.problems[0].reason)

    def test_z_suffix_parses_as_utc(self):
        dt = parse_event_time("2026-11-14T00:00:00Z")
        self.assertEqual(dt.utcoffset().total_seconds(), 0)

    def test_response_requires_fields_and_keeps_no_body(self):
        p = self.path("resp.jsonl")
        bad = ev("r", "q", 1, 2, "response", "2026-11-14T00:00:01Z",
                 status_code=200)  # missing response_bytes
        write_jsonl(p, [
            ev("r", "q", 1, 1, "queued", "2026-11-14T00:00:00Z"), bad])
        with self.ledger() as l:
            with self.assertRaises(ValidationError) as cm:
                l.import_file(p)
        self.assertIn("response_bytes", cm.exception.problems[0].reason)
        self.assertEqual(cm.exception.problems[0].request_id, "q")

    def test_duplicate_json_key_rejected(self):
        p = self.path("dupkey.jsonl")
        write_jsonl(p, [], raw_lines=[
            '{"run_id":"r","run_id":"s","request_id":"q","attempt":1,'
            '"seq":1,"event_id":"e1","event_type":"queued",'
            '"event_time":"2026-11-14T00:00:00Z"}'])
        with self.ledger() as l:
            with self.assertRaises(ValidationError) as cm:
                l.import_file(p)
        self.assertIn("duplicate JSON object key",
                      cm.exception.problems[0].reason)

    def test_unknown_type_and_bool_as_int(self):
        p = self.path("types.jsonl")
        write_jsonl(p, [], raw_lines=[
            json.dumps(ev("r", "q", 1, 1, "nope",
                          "2026-11-14T00:00:00Z")),
        ])
        with self.ledger() as l:
            with self.assertRaises(ValidationError) as cm:
                l.import_file(p)
        self.assertIn("event_type", cm.exception.problems[0].reason)

        p2 = self.path("bool.jsonl")
        e = ev("r", "q2", 1, 1, "queued", "2026-11-14T00:00:00Z")
        e["attempt"] = True
        write_jsonl(p2, [e])
        with self.ledger() as l:
            with self.assertRaises(ValidationError):
                l.import_file(p2)


class StateMachineTests(EventLedgerTestBase):
    def _happy_path_events(self, run="r", req="q"):
        t = "2026-11-14T00:00:{:02d}Z"
        return [
            ev(run, req, 1, 1, "queued", t.format(0)),
            ev(run, req, 1, 2, "started", t.format(1)),
            ev(run, req, 1, 3, "response", t.format(2),
               status_code=500, response_bytes=10),
            ev(run, req, 1, 4, "retry_scheduled", t.format(3),
               next_attempt=2),
            ev(run, req, 2, 5, "started", t.format(4)),
            ev(run, req, 2, 6, "response", t.format(5),
               status_code=200, response_bytes=42),
        ]

    def test_retry_success_timeline_and_duration(self):
        p = self.path("ok.jsonl")
        write_jsonl(p, self._happy_path_events())
        with self.ledger() as l:
            r = l.import_file(p)
            self.assertEqual(r.events_inserted, 6)
            tl = l.request_timeline("r", "q")
        self.assertEqual(tl.outcome, OUTCOME_SUCCESS)
        self.assertEqual(tl.attempt_count, 2)
        self.assertEqual(tl.final_status_code, 200)
        self.assertEqual(tl.duration_seconds, 4.0)
        self.assertEqual([e["seq"] for e in tl.events], [1, 2, 3, 4, 5, 6])

    def test_waiting_retry_then_success_across_files_out_of_order(self):
        a = self.path("a.jsonl")
        b = self.path("b.jsonl")
        c = self.path("c.jsonl")
        events = self._happy_path_events()
        # File a: queue + first attempt up to retry_scheduled.
        write_jsonl(a, [events[0], events[2], events[1], events[3]])
        with self.ledger() as l:
            l.import_file(a)
            tl = l.request_timeline("r", "q")
            self.assertEqual(tl.outcome, OUTCOME_WAITING_RETRY)
            self.assertEqual(tl.waiting_next_attempt, 2)
            self.assertIsNone(tl.duration_seconds)
            s = l.run_summary("r")
            self.assertEqual(s.waiting_retry, 1)
            self.assertEqual(s.decided, 0)
            self.assertIsNone(s.success_rate)
        # File c arrives before b (out of order files): retry started only.
        write_jsonl(c, [events[4]])
        with self.ledger() as l:
            l.import_file(c)
            tl = l.request_timeline("r", "q")
            self.assertEqual(tl.outcome, OUTCOME_RUNNING)
        # File b: the terminal response.
        write_jsonl(b, [events[5]])
        with self.ledger() as l:
            l.import_file(b)
            tl = l.request_timeline("r", "q")
            s = l.run_summary("r")
        self.assertEqual(tl.outcome, OUTCOME_SUCCESS)
        self.assertEqual(s.success, 1)
        self.assertEqual(s.decided, 1)
        self.assertEqual(s.success_rate, 1.0)
        self.assertEqual(s.durations_seconds, [4.0])

    def test_duplicate_seq_rejected_with_location(self):
        p = self.path("dupseq.jsonl")
        write_jsonl(p, [
            ev("r", "q", 1, 1, "queued", "2026-11-14T00:00:00Z"),
            ev("r", "q", 1, 1, "started", "2026-11-14T00:00:01Z",
               **{"event_id": "other"}),
        ])
        with self.ledger() as l:
            with self.assertRaises(ValidationError) as cm:
                l.import_file(p)
        prob = cm.exception.problems[0]
        self.assertEqual(prob.line, 2)
        self.assertEqual(prob.request_id, "q")
        self.assertIn("duplicate seq", prob.reason)

    def test_duplicate_seq_across_batches_rejected(self):
        p1 = self.path("p1.jsonl")
        write_jsonl(p1, self._happy_path_events()[:2])
        p2 = self.path("p2.jsonl")
        write_jsonl(p2, [ev("r", "q", 1, 2, "response",
                            "2026-11-14T00:00:09Z",
                            status_code=200, response_bytes=1,
                            **{"event_id": "new-id"})])
        with self.ledger() as l:
            l.import_file(p1)
            with self.assertRaises(ValidationError) as cm:
                l.import_file(p2)
        self.assertIn("already exists", cm.exception.problems[0].reason)

    def test_skipped_attempt(self):
        p = self.path("skip.jsonl")
        write_jsonl(p, [
            ev("r", "q", 1, 1, "queued", "2026-11-14T00:00:00Z"),
            ev("r", "q", 1, 2, "started", "2026-11-14T00:00:01Z"),
            ev("r", "q", 1, 3, "response", "2026-11-14T00:00:02Z",
               status_code=500, response_bytes=1),
            ev("r", "q", 1, 4, "retry_scheduled",
               "2026-11-14T00:00:03Z", next_attempt=2),
            ev("r", "q", 3, 5, "started", "2026-11-14T00:00:04Z"),
        ])
        with self.ledger() as l:
            with self.assertRaises(ValidationError) as cm:
                l.import_file(p)
        self.assertTrue(any("skipped attempt" in p.reason
                            for p in cm.exception.problems))

    def test_retry_scheduled_must_point_next_attempt(self):
        p = self.path("badnext.jsonl")
        write_jsonl(p, [
            ev("r", "q", 1, 1, "queued", "2026-11-14T00:00:00Z"),
            ev("r", "q", 1, 2, "started", "2026-11-14T00:00:01Z"),
            ev("r", "q", 1, 3, "response", "2026-11-14T00:00:02Z",
               status_code=500, response_bytes=1),
            ev("r", "q", 1, 4, "retry_scheduled",
               "2026-11-14T00:00:03Z", next_attempt=3),
        ])
        with self.ledger() as l:
            with self.assertRaises(ValidationError) as cm:
                l.import_file(p)
        self.assertTrue(any("next_attempt" in p.reason
                            for p in cm.exception.problems))

    def test_started_without_queued(self):
        p = self.path("noqueue.jsonl")
        write_jsonl(p, [ev("r", "q", 1, 1, "started",
                           "2026-11-14T00:00:00Z")])
        with self.ledger() as l:
            with self.assertRaises(ValidationError) as cm:
                l.import_file(p)
        self.assertIn("first event", cm.exception.problems[0].reason)

    def test_events_after_terminal(self):
        base = [
            ev("r", "q", 1, 1, "queued", "2026-11-14T00:00:00Z"),
            ev("r", "q", 1, 2, "started", "2026-11-14T00:00:01Z"),
            ev("r", "q", 1, 3, "response", "2026-11-14T00:00:02Z",
               status_code=200, response_bytes=5),
        ]
        # Late event after terminal response in a second file.
        p1 = self.path("t1.jsonl")
        p2 = self.path("t2.jsonl")
        write_jsonl(p1, base)
        write_jsonl(p2, [ev("r", "q", 1, 4, "started",
                            "2026-11-14T00:00:03Z",
                            **{"event_id": "late"})])
        with self.ledger() as l:
            l.import_file(p1)
            with self.assertRaises(ValidationError) as cm:
                l.import_file(p2)
        self.assertTrue(any("after terminal" in p.reason
                            for p in cm.exception.problems))
        # And after failed.
        p3 = self.path("t3.jsonl")
        p4 = self.path("t4.jsonl")
        write_jsonl(p3, [
            ev("r", "f", 1, 1, "queued", "2026-11-14T00:00:00Z"),
            ev("r", "f", 1, 2, "started", "2026-11-14T00:00:01Z"),
            ev("r", "f", 1, 3, "failed", "2026-11-14T00:00:02Z",
               error="boom"),
        ])
        write_jsonl(p4, [ev("r", "f", 1, 4, "response",
                            "2026-11-14T00:00:03Z",
                            status_code=200, response_bytes=1,
                            **{"event_id": "late2"})])
        with self.ledger() as l:
            l.import_file(p3)
            with self.assertRaises(ValidationError) as cm:
                l.import_file(p4)
        self.assertTrue(any("after terminal" in p.reason
                            for p in cm.exception.problems))

    def test_unfinished_requests_never_failed_or_timed(self):
        p = self.path("open.jsonl")
        write_jsonl(p, [
            ev("r", "q1", 1, 1, "queued", "2026-11-14T00:00:00Z"),
            ev("r", "q2", 1, 1, "queued", "2026-11-14T00:00:00Z"),
            ev("r", "q2", 1, 2, "started", "2026-11-14T00:00:01Z"),
        ])
        with self.ledger() as l:
            l.import_file(p)
            self.assertEqual(l.request_timeline("r", "q1").outcome,
                             OUTCOME_QUEUED)
            tl2 = l.request_timeline("r", "q2")
            self.assertEqual(tl2.outcome, OUTCOME_RUNNING)
            self.assertIsNone(tl2.duration_seconds)
            s = l.run_summary("r")
            self.assertEqual((s.queued, s.running, s.failed), (1, 1, 0))
            self.assertIsNone(s.success_rate)

    def test_cancelled_and_failed_outcomes(self):
        p = self.path("ends.jsonl")
        write_jsonl(p, [
            ev("r", "c", 1, 1, "queued", "2026-11-14T00:00:00Z"),
            ev("r", "c", 1, 2, "cancelled", "2026-11-14T00:00:01Z",
               reason="operator abort"),
            ev("r", "f", 1, 1, "queued", "2026-11-14T00:00:00Z"),
            ev("r", "f", 1, 2, "started", "2026-11-14T00:00:01Z"),
            ev("r", "f", 1, 3, "failed", "2026-11-14T00:00:05Z",
               error="connection reset"),
        ])
        with self.ledger() as l:
            l.import_file(p)
            self.assertEqual(l.request_timeline("r", "c").outcome,
                             OUTCOME_CANCELLED)
            self.assertEqual(l.request_timeline("r", "f").outcome,
                             OUTCOME_FAILED)
            s = l.run_summary("r")
            self.assertEqual(s.cancelled, 1)
            self.assertEqual(s.failed, 1)
            self.assertEqual(s.decided, 1)  # cancelled excluded from rate
            self.assertEqual(s.success_rate, 0.0)
            self.assertEqual(s.durations_seconds, [4.0])  # failed only

    def test_timezone_offset_duration(self):
        p = self.path("tz.jsonl")
        write_jsonl(p, [
            ev("r", "q", 1, 1, "queued",
               "2026-11-14T03:00:00+03:00"),          # 00:00 UTC
            ev("r", "q", 1, 2, "started",
               "2026-11-13T19:00:00-05:00"),          # 00:00 UTC
            ev("r", "q", 1, 3, "response",
               "2026-11-14T02:00:30+02:00",           # 00:00:30 UTC
               status_code=200, response_bytes=9),
        ])
        with self.ledger() as l:
            l.import_file(p)
            tl = l.request_timeline("r", "q")
        self.assertEqual(tl.duration_seconds, 30.0)
        # original strings preserved verbatim
        self.assertEqual(tl.events[1]["event_time"],
                         "2026-11-13T19:00:00-05:00")


class IdentityAndAtomicityTests(EventLedgerTestBase):
    def test_replay_identical_batch_is_noop(self):
        p = self.path("batch.jsonl")
        events = [
            ev("r", "q", 1, 1, "queued", "2026-11-14T00:00:00Z"),
            ev("r", "q", 1, 2, "started", "2026-11-14T00:00:01Z"),
        ]
        write_jsonl(p, events)
        with self.ledger() as l:
            r1 = l.import_file(p)
            r2 = l.import_file(p)
            self.assertEqual(r1.events_inserted, 2)
            self.assertEqual((r2.events_inserted, r2.events_replayed),
                             (0, 2))
            self.assertEqual(l.event_count(), 2)

    def test_same_event_id_different_content_rejects_batch(self):
        p1 = self.path("p1.jsonl")
        p2 = self.path("p2.jsonl")
        write_jsonl(p1, [ev("r", "q", 1, 1, "queued",
                            "2026-11-14T00:00:00Z")])
        changed = ev("r", "q", 1, 9, "queued",
                     "2026-11-14T00:00:00Z",
                     **{"event_id": "r-q-1"})  # reuse known id, new content
        write_jsonl(p2, [changed])  # same event_id ...-1, different seq
        with self.ledger() as l:
            l.import_file(p1)
            with self.assertRaises(EventConflictError):
                l.import_file(p2)
            self.assertEqual(l.event_count(), 1)

    def test_failed_batch_leaves_no_partial_state(self):
        good = self.path("good.jsonl")
        write_jsonl(good, [
            ev("r", "q", 1, 1, "queued", "2026-11-14T00:00:00Z"),
            ev("r", "q", 1, 2, "started", "2026-11-14T00:00:01Z"),
        ])
        bad = self.path("bad.jsonl")
        write_jsonl(bad, [
            ev("r", "x", 1, 1, "queued", "2026-11-14T00:00:00Z"),
            ev("r", "x", 1, 2, "response", "2026-11-14T00:00:01Z",
               status_code=200, response_bytes=1),  # no started
        ])
        with self.ledger() as l:
            l.import_file(good)
            before = l.event_count()
            with self.assertRaises(ValidationError):
                l.import_file(bad)
            self.assertEqual(l.event_count(), before)
            with self.assertRaises(LedgerNotFoundError):
                l.request_timeline("r", "x")

    def test_pre_commit_hook_failure_rolls_back(self):
        p = self.path("hook.jsonl")
        write_jsonl(p, [ev("r", "q", 1, 1, "queued",
                           "2026-11-14T00:00:00Z")])

        def boom():
            raise RuntimeError("simulated forced exit before commit")

        with self.ledger() as l:
            with self.assertRaises(RuntimeError):
                l.import_file(p, pre_commit_hook=boom)
            self.assertEqual(l.event_count(), 0)
            # Safe to re-import after fixing/restarting.
            r = l.import_file(p)
            self.assertEqual(r.events_inserted, 1)

    def test_hard_kill_before_commit_leaves_no_rows(self):
        # Separate process killed (SIGKILL) while inside the transaction.
        p = self.path("killed.jsonl")
        write_jsonl(p, [ev("r", "q", 1, 1, "queued",
                           "2026-11-14T00:00:00Z")])
        worker = Path(__file__).parent / "_kill_worker.py"
        proc = subprocess.run(
            [sys.executable, "-I", str(worker), str(self.db), str(p)],
            capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        with self.ledger() as l:
            self.assertEqual(l.event_count(), 0)
            r = l.import_file(p)  # restart + re-import works
            self.assertEqual(r.events_inserted, 1)
        # WAL/journal leftovers must not corrupt reopen.
        with sqlite3.connect(self.db) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        self.assertEqual(integrity, "ok")


class CrossRunAndExportTests(EventLedgerTestBase):
    def test_same_request_id_independent_per_run(self):
        p = self.path("runs.jsonl")
        write_jsonl(p, [
            ev("runA", "req1", 1, 1, "queued",
               "2026-11-14T00:00:00Z"),
            ev("runB", "req1", 1, 1, "queued",
               "2026-11-14T00:00:00Z"),
            ev("runB", "req1", 1, 2, "started",
               "2026-11-14T00:00:01Z"),
        ])
        with self.ledger() as l:
            l.import_file(p)
            self.assertEqual(l.request_timeline("runA", "req1").outcome,
                             OUTCOME_QUEUED)
            self.assertEqual(l.request_timeline("runB", "req1").outcome,
                             OUTCOME_RUNNING)
            self.assertEqual(l.run_summary("runA").requests_total, 1)
            self.assertEqual(l.run_summary("runB").requests_total, 1)

    def test_stable_export_and_reimport_is_idempotent(self):
        p = self.path("in.jsonl")
        events = [
            ev("r", "q", 1, 1, "queued", "2026-11-14T00:00:00Z",
               note="保留中文字段"),
            ev("r", "q", 1, 2, "started", "2026-11-14T00:00:01Z"),
            ev("r", "q", 1, 3, "response", "2026-11-14T00:00:02Z",
               status_code=200, response_bytes=7),
        ]
        write_jsonl(p, events)
        out1, out2 = self.path("e1.jsonl"), self.path("e2.jsonl")
        with self.ledger() as l:
            l.import_file(p)
            n1 = l.export_jsonl(out1)
            original = {json.dumps(e, sort_keys=True) for e in events}
            exported = {json.dumps(json.loads(line), sort_keys=True)
                        for line in open(out1, encoding="utf-8")}
            self.assertEqual(original, exported)  # field-for-field content
        # Re-import the export into a fresh ledger and export again.
        db2 = self.path("ledger2.db")
        with Ledger(db2) as l2:
            r = l2.import_file(out1)
            self.assertEqual(r.events_inserted, n1)
            l2.export_jsonl(out2)
        self.assertEqual(open(out1, "rb").read(), open(out2, "rb").read())
        with Ledger(db2) as l2:
            self.assertEqual(l2.import_file(out2).events_inserted, 0)

    def test_original_fields_preserved_including_extras(self):
        p = self.path("extra.jsonl")
        e = ev("r", "q", 1, 1, "queued", "2026-11-14T00:00:00Z",
               worker="w-7", priority=3)
        write_jsonl(p, [e])
        with self.ledger() as l:
            l.import_file(p)
            tl = l.request_timeline("r", "q")
        self.assertEqual(tl.events[0]["worker"], "w-7")
        self.assertEqual(tl.events[0]["priority"], 3)


class EdgeCaseTests(EventLedgerTestBase):
    def test_intra_batch_exact_duplicate_line_is_replay(self):
        p = self.path("intra.jsonl")
        one = ev("r", "q", 1, 1, "queued", "2026-11-14T00:00:00Z")
        write_jsonl(p, [one, dict(one)])
        with self.ledger() as l:
            r = l.import_file(p)
        self.assertEqual((r.events_inserted, r.events_replayed), (1, 1))
        with self.ledger() as l:
            self.assertEqual(l.event_count(), 1)

    def test_intra_batch_same_id_different_content_rejected(self):
        p = self.path("intraconf.jsonl")
        first = ev("r", "q", 1, 1, "queued", "2026-11-14T00:00:00Z")
        second = ev("r", "q", 1, 2, "started",
                    "2026-11-14T00:00:01Z", **{"event_id": first["event_id"]})
        write_jsonl(p, [first, second])
        with self.ledger() as l:
            with self.assertRaises(EventConflictError) as cm:
                l.import_file(p)
        self.assertIn("different content",
                      cm.exception.problems[0].reason)

    def test_empty_file_rejected_and_nothing_written(self):
        p = self.path("empty.jsonl")
        write_jsonl(p, [])
        with self.ledger() as l:
            with self.assertRaises(ValidationError) as cm:
                l.import_file(p)
        self.assertIn("no events", cm.exception.problems[0].reason)

    def test_multiple_problems_collected_together(self):
        p = self.path("multi.jsonl")
        write_jsonl(p, [
            ev("r", "q1", 1, 1, "nope", "2026-11-14T00:00:00Z"),
            ev("r", "q2", 1, 1, "queued", "not-a-timestamp"),
        ])
        with self.ledger() as l:
            with self.assertRaises(ValidationError) as cm:
                l.import_file(p)
        self.assertEqual(len(cm.exception.problems), 2)
        self.assertEqual({p.line for p in cm.exception.problems}, {1, 2})

    def test_late_started_backfills_after_corrected_reimport(self):
        # A batch missing the started event is rejected wholesale; once
        # the operator supplies the missing earlier event, the corrected
        # batch imports and reconstructs the full timeline.
        broken = self.path("broken.jsonl")
        write_jsonl(broken, [
            ev("r", "z", 1, 1, "queued", "2026-11-14T00:00:00Z"),
            ev("r", "z", 1, 3, "response", "2026-11-14T00:00:02Z",
               status_code=200, response_bytes=1),
        ])
        fixed = self.path("fixed.jsonl")
        write_jsonl(fixed, [
            ev("r", "z", 1, 1, "queued", "2026-11-14T00:00:00Z"),
            ev("r", "z", 1, 2, "started", "2026-11-14T00:00:01Z"),
            ev("r", "z", 1, 3, "response", "2026-11-14T00:00:02Z",
               status_code=200, response_bytes=1),
        ])
        with self.ledger() as l:
            with self.assertRaises(ValidationError):
                l.import_file(broken)
            self.assertEqual(l.event_count(), 0)
            l.import_file(fixed)
            tl = l.request_timeline("r", "z")
        self.assertEqual(tl.outcome, OUTCOME_SUCCESS)
        self.assertEqual(tl.duration_seconds, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
