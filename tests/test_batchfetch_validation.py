"""Manifest and configuration validation tests (no network, no threads).

Every rejection here happens before any socket could be opened.
"""

from __future__ import annotations

import io
import unittest

from batchfetch import (BatchConfig, BatchConfigError, ManifestError,
                        OriginBudget, config_from_mapping, parse_manifest,
                        read_manifest)


def lines(*objects) -> io.StringIO:
    import json
    return io.StringIO("\n".join(json.dumps(obj) for obj in objects))


class ManifestTests(unittest.TestCase):
    def test_accepts_valid_manifest_in_order(self):
        jobs = parse_manifest(lines(
            {"job_id": "a", "url": "http://a.test/x"},
            {"job_id": "b", "url": "https://b.test/y?z=1"},
        ))
        self.assertEqual([j.job_id for j in jobs], ["a", "b"])
        self.assertEqual([j.index for j in jobs], [0, 1])
        self.assertEqual(jobs[0].origin, "http://a.test")
        # Path and query are preserved byte for byte.
        self.assertEqual(jobs[1].url, "https://b.test/y?z=1")
        self.assertEqual(jobs[1].raw_url, "https://b.test/y?z=1")

    def test_rejects_blank_and_non_json_lines(self):
        with self.assertRaises(ManifestError):
            parse_manifest(io.StringIO("\n"))
        with self.assertRaises(ManifestError):
            parse_manifest(io.StringIO('{"job_id": "a", "url": "x"}\nnot-json\n'))

    def test_rejects_non_object_and_wrong_fields(self):
        with self.assertRaises(ManifestError):
            parse_manifest(io.StringIO('["job_id", "url"]\n'))
        with self.assertRaises(ManifestError):
            parse_manifest(io.StringIO('{"job_id": "a"}\n'))
        with self.assertRaises(ManifestError):
            parse_manifest(
                io.StringIO('{"job_id": "a", "url": "http://a.test/", '
                            '"extra": 1}\n'))

    def test_rejects_bad_or_duplicate_job_id(self):
        with self.assertRaises(ManifestError):
            parse_manifest(lines({"job_id": "", "url": "http://a.test/"}))
        with self.assertRaises(ManifestError):
            parse_manifest(lines({"job_id": "a\nb", "url": "http://a.test/"}))
        with self.assertRaises(ManifestError):
            parse_manifest(lines(
                {"job_id": "a", "url": "http://a.test/"},
                {"job_id": "a", "url": "http://b.test/"},
            ))

    def test_rejects_illegal_urls(self):
        for bad in ("ftp://a.test/", "not a url", "http://a.test/#frag",
                    "https://u:p@a.test/", "http:///x",
                    "https://a.test/a b"):
            with self.assertRaises(ManifestError, msg=bad):
                parse_manifest(lines({"job_id": "j", "url": bad}))

    def test_scheme_and_port_split_budgets(self):
        jobs = parse_manifest(lines(
            {"job_id": "1", "url": "http://a.test:80/x"},
            {"job_id": "2", "url": "https://a.test:443/x"},
            {"job_id": "3", "url": "http://a.test:8080/x"},
        ))
        origins = {j.job_id: j.origin for j in jobs}
        self.assertEqual(origins["1"], "http://a.test")
        self.assertEqual(origins["2"], "https://a.test")
        self.assertEqual(origins["3"], "http://a.test:8080")
        self.assertEqual(len({j.origin for j in jobs}), 3)

    def test_empty_manifest_rejected(self):
        with self.assertRaises(ManifestError):
            parse_manifest(io.StringIO(""))

    def test_read_manifest_utf8_and_missing_file(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "jobs.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"job_id": "中", "url": "http://a.test/中"}\n')
            jobs = read_manifest(path)
            self.assertEqual(jobs[0].job_id, "中")
            with self.assertRaises(ManifestError):
                read_manifest(os.path.join(tmp, "missing.jsonl"))


class ConfigTests(unittest.TestCase):
    def _base(self, **over):
        values = {"global_inflight": 4, "origin_inflight": 2,
                  "origin_min_interval": 0.25}
        values.update(over)
        return values

    def test_defaults_and_round_trip(self):
        cfg = config_from_mapping(self._base())
        self.assertEqual(cfg.global_inflight, 4)
        self.assertEqual(cfg.budget_for("http://x.test").inflight_cap, 2)
        self.assertEqual(
            cfg.budget_for("http://x.test").min_interval_seconds, 0.25)
        payload = cfg.to_dict()
        self.assertEqual(payload["global_inflight"], 4)
        self.assertEqual(payload["max_redirects"], 5)

    def test_non_positive_concurrency_rejected(self):
        for bad in (0, -1):
            with self.assertRaises(BatchConfigError):
                config_from_mapping(self._base(global_inflight=bad))
            with self.assertRaises(BatchConfigError):
                config_from_mapping(self._base(origin_inflight=bad))
        with self.assertRaises(BatchConfigError):
            config_from_mapping(self._base(global_inflight="2"))
        with self.assertRaises(BatchConfigError):
            config_from_mapping(self._base(global_inflight=True))

    def test_other_invalid_configuration(self):
        with self.assertRaises(BatchConfigError):
            config_from_mapping(self._base(request_timeout_seconds=0))
        with self.assertRaises(BatchConfigError):
            config_from_mapping(self._base(request_timeout_seconds=-1))
        with self.assertRaises(BatchConfigError):
            config_from_mapping(self._base(max_body_bytes=0))
        with self.assertRaises(BatchConfigError):
            config_from_mapping(self._base(max_redirects=-1))
        with self.assertRaises(BatchConfigError):
            config_from_mapping(self._base(origin_min_interval=-0.1))
        with self.assertRaises(BatchConfigError):
            config_from_mapping({"origin_inflight": 1})  # missing global
        with self.assertRaises(BatchConfigError):
            config_from_mapping(self._base(unknown_key=1))

    def test_origin_overrides_validated_and_canonicalized(self):
        cfg = config_from_mapping(self._base(
            origin_overrides={
                "http://A.TEST:80/": {"inflight_cap": 1,
                                      "min_interval_seconds": 1.5}}))
        self.assertIn("http://a.test", cfg.origin_overrides)
        self.assertEqual(cfg.budget_for("http://a.test").inflight_cap, 1)
        with self.assertRaises(BatchConfigError):
            config_from_mapping(self._base(
                origin_overrides={"not an origin":
                                  {"inflight_cap": 1}}))
        with self.assertRaises(BatchConfigError):
            config_from_mapping(self._base(
                origin_overrides={"http://a.test/":
                                  {"inflight_cap": 0}}))

    def test_direct_dataclass_validation(self):
        with self.assertRaises(BatchConfigError):
            BatchConfig(
                global_inflight=0,
                default_origin=OriginBudget(1, 0.0)).validated()
        with self.assertRaises(BatchConfigError):
            BatchConfig(
                global_inflight=1,
                default_origin=OriginBudget(1, -0.5)).validated()


if __name__ == "__main__":
    unittest.main()
