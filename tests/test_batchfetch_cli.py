"""End-to-end CLI tests: real subprocess, real localhost HTTP, SIGINT."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from batchfetch.testsite import DualPortSite

REPO = Path(__file__).resolve().parent.parent
CLI = [sys.executable, "-I", "-c",
       "import sys; sys.path.insert(0, %r); "
       "from batchfetch.__main__ import main; "
       "sys.exit(main(sys.argv[1:]))" % str(REPO)]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.site = DualPortSite().start()
        self.addCleanup(self.site.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manifest = Path(self.tmp.name) / "jobs.jsonl"
        self.report = Path(self.tmp.name) / "report.jsonl"

    def _write_manifest(self, *jobs) -> None:
        with open(self.manifest, "w", encoding="utf-8") as fh:
            for jid, url in jobs:
                fh.write(json.dumps({"job_id": jid, "url": url}) + "\n")

    def _run(self, *extra_args) -> subprocess.CompletedProcess:
        args = CLI + ["run", "--manifest", str(self.manifest),
                      "--report", str(self.report),
                      "--global-inflight", "4", "--origin-inflight", "2",
                      "--origin-min-interval", "0.02",
                      "--request-timeout", "5", "--max-body-bytes", "4096",
                      "--max-redirects", "3"] + list(extra_args)
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=30)

    def test_success_exit_zero_and_report_shape(self):
        self._write_manifest(
            ("a", self.site.origin_a + "/ok"),
            ("b", self.site.origin_b + "/empty"),
            ("c", self.site.origin_a + "/fail500"))
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(summary["complete"], 3)
        self.assertTrue(summary["success"])
        lines = [json.loads(line) for line in
                 self.report.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(lines[0]["type"], "summary")
        results = [line for line in lines if line["type"] == "result"]
        self.assertEqual([r["job_id"] for r in results], ["a", "b", "c"])
        self.assertTrue(
            any(line["type"] == "timeline" for line in lines))

    def test_failure_batch_exits_three_but_keeps_all_results(self):
        self._write_manifest(
            ("dead", "http://127.0.0.1:1/x"),
            ("alive", self.site.origin_b + "/ok"))
        proc = self._run("--request-timeout", "2")
        self.assertEqual(proc.returncode, 3, proc.stdout)
        lines = [json.loads(line) for line in
                 self.report.read_text(encoding="utf-8").splitlines()]
        results = {line["job_id"]: line
                   for line in lines if line["type"] == "result"}
        self.assertEqual(results["dead"]["outcome"], "failed")
        self.assertEqual(results["alive"]["outcome"], "complete")
        self.assertEqual(len(results), 2)

    def test_invalid_manifest_exits_two_before_network(self):
        self.manifest.write_text(
            '{"job_id": "a", "url": "not-a-url"}\n', encoding="utf-8")
        proc = self._run()
        self.assertEqual(proc.returncode, 2)
        payload = json.loads(proc.stderr.strip())
        self.assertIn("Error", payload["error"])
        self.assertFalse(self.report.exists())

    def test_invalid_config_exits_two(self):
        self._write_manifest(("a", self.site.origin_a + "/ok"))
        proc = self._run("--global-inflight", "0")
        self.assertEqual(proc.returncode, 2)

    def test_origin_budget_override_flag(self):
        self._write_manifest(
            ("a1", self.site.origin_a + "/ok/1"),
            ("a2", self.site.origin_a + "/ok/2"))
        override = f"{self.site.origin_a}:1:0.0"
        proc = self._run("--origin-budget", override)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_sigint_cancels_and_exits_130(self):
        gated = self.site.origin_a + "/gate?name=sig"
        queued = self.site.origin_b + "/ok"
        self._write_manifest(("gated", gated), ("queued", queued))
        args = CLI + ["run", "--manifest", str(self.manifest),
                      "--report", str(self.report),
                      "--global-inflight", "1", "--origin-inflight", "1",
                      "--origin-min-interval", "0.0",
                      "--request-timeout", "10"]
        proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        # Wait for the gated request to reach the server.
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if any(r["path"].startswith("/gate")
                   for r in self.site.receipts()):
                break
            time.sleep(0.02)
        proc.send_signal(signal.SIGINT)
        # Release the gate so the in-flight exchange can finish and the
        # process can write its report and exit 130.
        with urllib.request.urlopen(
                self.site.control_url("a", "open", name="sig"),
                timeout=5) as resp:
            resp.read()
        stdout, stderr = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 130, stderr)
        lines = [json.loads(line) for line in
                 self.report.read_text(encoding="utf-8").splitlines()]
        results = {line["job_id"]: line
                   for line in lines if line["type"] == "result"}
        self.assertEqual(results["queued"]["outcome"], "cancelled")
        self.assertTrue(
            any(line.get("kind") == "cancelled"
                for line in lines if line["type"] == "timeline"))
        # The queued job never reached the server.
        self.assertFalse(
            any("/ok" in r["path"]
                for r in self.site.receipts()
                if not r["path"].startswith("/__control")))


if __name__ == "__main__":
    unittest.main()
