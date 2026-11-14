"""Real HTTP integration tests against the two-port localhost site.

These use the real :func:`~batchfetch.exchange.perform_exchange`, real
sockets and the real monotonic clock; the only server is the in-process
:class:`~batchfetch.testsite.DualPortSite` bound to 127.0.0.1.
"""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from batchfetch import (CANCELLED, COMPLETE, FAILED, BatchConfig, BatchRunner,
                        OriginBudget)
from batchfetch.config import JobSpec, parse_manifest
from batchfetch.testsite import DualPortSite


def config(**over) -> BatchConfig:
    values = dict(global_inflight=4,
                  default_origin=OriginBudget(2, 0.05),
                  request_timeout_seconds=5.0,
                  max_body_bytes=4096, max_redirects=4)
    values.update(over)
    return BatchConfig(**values).validated()


def jobs_from(site: DualPortSite, *specs) -> list[JobSpec]:
    lines = [json.dumps({"job_id": jid, "url": url})
             for jid, url in specs]
    return parse_manifest(io.StringIO("\n".join(lines)))


class BasicFetchTests(unittest.TestCase):
    def setUp(self):
        self.site = DualPortSite().start()
        self.addCleanup(self.site.stop)

    def test_ok_empty_500_and_streaming_digest(self):
        jobs = jobs_from(
            self.site,
            ("ok", self.site.origin_a + "/ok/path?x=1"),
            ("empty", self.site.origin_b + "/empty"),
            ("fail", self.site.origin_a + "/fail500"),
        )
        runner = BatchRunner(jobs, config()).run()
        results = {r.job_id: r for r in runner.iter_results()}
        self.assertEqual(results["ok"].outcome, COMPLETE)
        self.assertEqual(results["ok"].status, 200)
        self.assertEqual(results["empty"].outcome, COMPLETE)
        self.assertEqual(results["empty"].status, 200)
        self.assertEqual(results["empty"].chain[0].body_bytes, 0)
        self.assertEqual(results["empty"].chain[0].body_sha256,
                         hashlib.sha256(b"").hexdigest())
        # 500 is an obtained HTTP response, not a fetch failure.
        self.assertEqual(results["fail"].outcome, COMPLETE)
        self.assertEqual(results["fail"].status, 500)
        self.assertIsNone(results["fail"].failure_kind)
        # The digest matches an independent calculation of the same body;
        # /fail500 has a fixed body so the hash is reproducible.
        try:
            with urllib.request.urlopen(jobs[2].url, timeout=5) as resp:
                expected = hashlib.sha256(resp.read()).hexdigest()
        except urllib.error.HTTPError as exc:
            expected = hashlib.sha256(exc.read()).hexdigest()
        self.assertEqual(results["fail"].chain[0].body_sha256, expected)

    def test_body_cap_trips_mid_stream_and_releases_slots(self):
        url = self.site.origin_a + "/oversize?len=100000"
        jobs = jobs_from(self.site, ("big", url),
                         ("after", self.site.origin_a + "/ok"))
        runner = BatchRunner(
            jobs, config(max_body_bytes=2048)).run()
        results = {r.job_id: r for r in runner.iter_results()}
        self.assertEqual(results["big"].outcome, FAILED)
        self.assertEqual(results["big"].failure_kind, "body_too_large")
        self.assertGreaterEqual(results["big"].chain[0].body_bytes, 2048)
        # The capped exchange released its slot: the sibling job completes.
        self.assertEqual(results["after"].outcome, COMPLETE)

    def test_declared_length_early_close_is_incomplete(self):
        url = self.site.origin_a + "/truncated?len=64"
        jobs = jobs_from(self.site, ("cut", url))
        runner = BatchRunner(jobs, config()).run()
        result = runner.iter_results()[0]
        self.assertEqual(result.outcome, FAILED)
        self.assertEqual(result.failure_kind, "truncated_body")
        self.assertGreater(result.chain[0].body_bytes, 0)

    def test_connection_error_is_failure_not_exception(self):
        # Nothing listens on this port: connect must fail cleanly.
        dead = "http://127.0.0.1:1/never"
        jobs = jobs_from(self.site, ("dead", dead),
                         ("alive", self.site.origin_b + "/ok"))
        runner = BatchRunner(
            jobs, config(request_timeout_seconds=2.0)).run()
        results = {r.job_id: r for r in runner.iter_results()}
        self.assertEqual(results["dead"].outcome, FAILED)
        self.assertEqual(results["dead"].failure_kind, "connection_error")
        self.assertEqual(results["alive"].outcome, COMPLETE)


class RedirectHttpTests(unittest.TestCase):
    def setUp(self):
        self.site = DualPortSite().start()
        self.addCleanup(self.site.stop)

    def test_redirect_chain_across_ports(self):
        target = self.site.origin_b + "/ok"
        url = (self.site.origin_a +
               f"/redirect?to={target}&status=307")
        jobs = jobs_from(self.site, ("j", url))
        runner = BatchRunner(jobs, config()).run()
        result = runner.iter_results()[0]
        self.assertEqual(result.outcome, COMPLETE)
        self.assertEqual(result.status, 200)
        self.assertEqual(result.final_url, target)
        self.assertEqual([h.status for h in result.chain], [307, 200])
        # Server-side evidence of both hops, one per origin.
        receipts = self.site.receipts()
        self.assertEqual(len(receipts), 2)
        self.assertEqual({r["port"] for r in receipts},
                         {self.site.port_a, self.site.port_b})

    def test_redirect_ring_fails_with_clear_reason(self):
        jobs = jobs_from(
            self.site, ("ring", self.site.origin_a + "/loop-a"))
        runner = BatchRunner(jobs, config(max_redirects=4)).run()
        result = runner.iter_results()[0]
        self.assertEqual(result.outcome, FAILED)
        self.assertEqual(result.failure_kind, "redirect_loop")
        self.assertGreaterEqual(len(result.chain), 2)

    def test_only_listed_statuses_are_followed(self):
        # 304 is not in the redirect set: it must come back as a final
        # HTTP response rather than being treated as a hop.
        url = (self.site.origin_a +
               "/redirect?to=/ok&status=304")
        jobs = jobs_from(self.site, ("j", url))
        runner = BatchRunner(jobs, config()).run()
        result = runner.iter_results()[0]
        self.assertEqual(result.outcome, COMPLETE)
        self.assertEqual(result.status, 304)
        self.assertEqual(len(result.chain), 1)


class PacingHttpTests(unittest.TestCase):
    def setUp(self):
        self.site = DualPortSite().start()
        self.addCleanup(self.site.stop)

    def test_actual_start_gap_matches_configured_interval(self):
        jobs = jobs_from(self.site, *(
            (f"a{i}", self.site.origin_a + f"/ok/{i}") for i in range(3)))
        runner = BatchRunner(
            jobs,
            config(default_origin=OriginBudget(3, 0.2))).run()
        starts = [e for e in runner.timeline
                  if e.kind == "exchange_start"
                  and e.origin == self.site.origin_a]
        self.assertEqual(len(starts), 3)
        gaps = [starts[i + 1].mono - starts[i].mono for i in range(2)]
        self.assertTrue(all(g >= 0.2 - 0.02 for g in gaps), gaps)

    def test_timeline_cross_checks_against_server_receipts(self):
        jobs = jobs_from(
            self.site,
            ("a", self.site.origin_a + "/ok"),
            ("b", self.site.origin_b + "/ok"),
            ("c", self.site.origin_a + "/ok/2"),
        )
        runner = BatchRunner(
            jobs,
            config(default_origin=OriginBudget(1, 0.15),
                   global_inflight=4)).run()
        starts = [(e.origin, e.url) for e in runner.timeline
                  if e.kind == "exchange_start"]
        server = [(f"http://127.0.0.1:{r['port']}",
                   f"http://127.0.0.1:{r['port']}"
                   + r["path"].split("?")[0])
                  for r in self.site.receipts()]
        # The executor's start set equals the server's receipt set.
        self.assertEqual(sorted(starts), sorted(server))
        # Every end follows its start and reports occupancy.
        starts_e = [e for e in runner.timeline
                    if e.kind == "exchange_start"]
        ends_e = [e for e in runner.timeline if e.kind == "exchange_end"]
        self.assertEqual(len(starts_e), len(ends_e))
        for event in starts_e:
            self.assertIn("occupancy", event.detail)
            self.assertIn("wait_reasons", event.detail)


class CancellationHttpTests(unittest.TestCase):
    def setUp(self):
        self.site = DualPortSite().start()
        self.addCleanup(self.site.stop)

    def test_cancel_while_exchange_gated(self):
        gated = self.site.origin_a + "/gate?name=g1"
        queued = self.site.origin_b + "/ok"
        jobs = jobs_from(self.site, ("gated", gated),
                         ("queued", queued))
        # Global cap 1: while the gated exchange holds the only slot the
        # second job cannot have started, so cancellation must catch it.
        runner = BatchRunner(
            jobs, config(global_inflight=1,
                         default_origin=OriginBudget(2, 0.0)))
        thread = threading.Thread(target=runner.run,
                                                 daemon=True)
        thread.start()
        # Wait until the gated request has actually reached the server
        # while its response is held behind the gate.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(r["path"].startswith("/gate")
                   for r in self.site.receipts()):
                break
            time.sleep(0.02)
        runner.cancel()
        time.sleep(0.2)
        def worker_receipts() -> list[dict]:
            return [r for r in self.site.receipts()
                    if not r["path"].startswith("/__control")]
        before_open = len(worker_receipts())
        # Release the gate: the in-flight exchange finishes its timeout
        # window and the runner must wind down with no dispatch thread.
        with urllib.request.urlopen(
                self.site.control_url("a", "open", name="g1"),
                timeout=5) as resp:
            resp.read()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        results = {r.job_id: r for r in runner.iter_results()}
        # The gated exchange was already in flight at cancel time, so it is
        # allowed to finish within its timeout and keeps its real response.
        self.assertEqual(results["gated"].outcome, COMPLETE)
        self.assertEqual(results["gated"].status, 200)
        # The never-started job is an explicit cancellation, not a 404.
        self.assertEqual(results["queued"].outcome, CANCELLED)
        self.assertIsNone(results["queued"].status)
        # No queued job request ever reached the server (control calls are
        # recorded too, but excluded here).
        self.assertEqual(len(worker_receipts()), before_open)
        self.assertFalse(
            any("/ok" in r["path"] for r in worker_receipts()))

    def test_new_batch_after_cancelled_batch_has_clean_state(self):
        gated = self.site.origin_a + "/gate?name=g2"
        runner1 = BatchRunner(
            jobs_from(self.site, ("g", gated)),
            config(global_inflight=1,
                   default_origin=OriginBudget(1, 0.0)))
        thread = threading.Thread(target=runner1.run,
                                                 daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(r["path"].startswith("/gate")
                   for r in self.site.receipts()):
                break
            time.sleep(0.02)
        runner1.cancel()
        with urllib.request.urlopen(
                self.site.control_url("a", "open", name="g2"),
                timeout=5) as resp:
            resp.read()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())

        # A fresh batch in the same process must fetch successfully.
        runner2 = BatchRunner(
            jobs_from(self.site, ("fresh", self.site.origin_b + "/ok")),
            config()).run()
        result = runner2.iter_results()[0]
        self.assertEqual(result.outcome, COMPLETE)
        self.assertEqual(result.status, 200)
        self.assertEqual(runner2.global_inflight, 0)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.site = DualPortSite().start()
        self.addCleanup(self.site.stop)

    def test_report_order_and_one_terminal_line_per_job(self):
        jobs = jobs_from(
            self.site,
            ("z", self.site.origin_a + "/fail500"),
            ("a", self.site.origin_b + "/empty"),
            ("m", self.site.origin_a + "/ok"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.jsonl"
            runner = BatchRunner(jobs, config()).run()
            runner.write_report(str(report))
            lines = [json.loads(line) for line in
                     report.read_text(encoding="utf-8").splitlines()]
        summary = lines[0]
        self.assertEqual(summary["type"], "summary")
        self.assertEqual(summary["jobs"], 3)
        results = [line for line in lines if line["type"] == "result"]
        self.assertEqual([r["job_id"] for r in results], ["z", "a", "m"])
        self.assertEqual(len({r["job_id"] for r in results}), 3)
        timeline = [line for line in lines if line["type"] == "timeline"]
        self.assertTrue(any(t["kind"] == "exchange_start"
                            for t in timeline))
        self.assertTrue(any(t["kind"] == "exchange_end"
                            for t in timeline))


if __name__ == "__main__":
    unittest.main()
