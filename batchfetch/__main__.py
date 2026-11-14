"""Command line interface.

    python -m batchfetch run --manifest jobs.jsonl --report report.jsonl \
        --global-inflight N --origin-inflight N [--origin-min-interval S] \
        [--request-timeout S] [--max-body-bytes N] [--max-redirects N] \
        [--origin-budget ORIGIN:CAP[:INTERVAL]] ... [--config CONFIG.json]

The manifest is a UTF-8 JSONL file with one ``{"job_id", "url"}`` object
per line.  The report is rewritten (never appended) and contains:

1. one summary line (counts of complete/failed/cancelled, config echo);
2. exactly one terminal result line per job_id, in manifest input order;
3. the actual exchange timeline (starts, ends, waits, cancellation).

Exit codes
----------
* 0 -- every job completed (HTTP 4xx/5xx count as completed responses);
* 2 -- configuration or manifest rejection, before any network use;
* 3 -- the batch finished but contains fetch failures or cancellations;
* 130 -- terminated by SIGINT (the report still captures the cancellation).
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from typing import List, Optional

from .config import (BatchConfig, OriginBudget, config_from_mapping,
                     read_manifest)
from .errors import BatchConfigError, ManifestError
from .scheduler import BatchRunner


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="batchfetch",
        description="Budgeted batch web fetcher: JSONL job list + per-origin "
                    "budgets -> real HTTP exchanges with a full result and "
                    "timeline report.  Standard library only.")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run one batch")
    r.add_argument("--manifest", required=True,
                   help="UTF-8 JSONL file, one {job_id, url} object per line")
    r.add_argument("--report", required=True,
                   help="report output path (rewritten, never appended)")
    r.add_argument("--config", default=None,
                   help="optional JSON file with configuration keys; CLI "
                        "flags override it")
    r.add_argument("--global-inflight", type=int, default=None,
                   help="maximum exchanges in flight across all origins")
    r.add_argument("--origin-inflight", type=int, default=None,
                   help="default per-origin concurrent exchange cap")
    r.add_argument("--origin-min-interval", type=float, default=None,
                   help="default minimum seconds between two request starts "
                        "of the same origin (monotonic clock)")
    r.add_argument("--origin-budget", action="append", default=[],
                   metavar="ORIGIN:CAP[:INTERVAL]",
                   help="per-origin override, e.g. http://a.test:8081:2:0.5; "
                        "repeatable")
    r.add_argument("--request-timeout", type=float, default=None,
                   help="seconds allowed for one whole HTTP exchange")
    r.add_argument("--max-body-bytes", type=int, default=None,
                   help="maximum accepted response body bytes")
    r.add_argument("--max-redirects", type=int, default=None,
                   help="maximum redirect hops per job")
    return p


def _parse_origin_budget(spec: str) -> tuple[str, OriginBudget]:
    # Form: scheme://host[:port]:cap[:interval].  The origin itself may
    # end in a numeric port, so we cannot simply peel numeric tail
    # segments: split at the last ':' that follows a non-numeric host
    # segment, which is exactly where the cap begins.
    if "://" not in spec:
        raise BatchConfigError(
            f"--origin-budget {spec!r} must look like "
            f"scheme://host[:port]:cap[:interval]")
    head, sep, tail = spec.partition("://")
    if tail.startswith("["):
        # IPv6 literal: [addr][:port]:cap[:interval]
        bracket_end = tail.find("]")
        if bracket_end < 0:
            raise BatchConfigError(
                f"--origin-budget {spec!r}: malformed IPv6 literal")
        remainder = tail[bracket_end + 1:]
        pieces = remainder.split(":")
        # pieces[0] == ""; optional port, then cap and interval.
        numbers = [p for p in pieces[1:] if p != ""]
        if len(numbers) < 1:
            raise BatchConfigError(
                f"--origin-budget {spec!r} is missing the inflight cap")
        origin = head + sep + tail[:bracket_end + 1]
        if len(numbers) >= 2:
            origin += ":" + numbers[0]
        cap = int(numbers[-2] if len(numbers) >= 2 else numbers[0])
        interval = float(numbers[-1]) if len(numbers) >= 2 else 0.0
        return origin, OriginBudget(cap, interval)
    tail_segments = tail.split(":")
    if len(tail_segments) < 2:
        raise BatchConfigError(
            f"--origin-budget {spec!r} is missing the inflight cap")
    # tail_segments = host, [port,] cap [, interval]; the host is the only
    # guaranteed non-numeric segment, so cap starts right after it (+1 for
    # no port, +2 when a port is present).
    host = tail_segments[0]
    cut = 2 if len(tail_segments) >= 3 else 1
    origin_tail, budget_tail = tail_segments[:cut], tail_segments[cut:]
    origin = head + sep + ":".join(origin_tail)
    try:
        cap = int(budget_tail[0])
    except (IndexError, ValueError) as exc:
        raise BatchConfigError(
            f"--origin-budget {spec!r}: inflight cap must be an integer"
        ) from exc
    interval = 0.0
    if len(budget_tail) > 1:
        try:
            interval = float(budget_tail[1])
        except ValueError as exc:
            raise BatchConfigError(
                f"--origin-budget {spec!r}: interval must be a number"
            ) from exc
    if not host:
        raise BatchConfigError(
            f"--origin-budget {spec!r} has an empty host")
    return origin, OriginBudget(cap, interval)


def _config_from_args(args) -> BatchConfig:
    values: dict = {}
    if args.config:
        try:
            with open(args.config, "r", encoding="utf-8") as fh:
                values.update(json.load(fh))
        except OSError as exc:
            raise BatchConfigError(
                f"cannot read config {args.config!r}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise BatchConfigError(
                f"config {args.config!r} is not valid JSON: {exc}") from exc
    overrides = dict(values.get("origin_overrides", {}))
    for spec in args.origin_budget:
        origin, budget = _parse_origin_budget(spec)
        merged = dict(overrides.get(origin, {}))
        merged["inflight_cap"] = budget.inflight_cap
        if budget.min_interval_seconds:
            merged["min_interval_seconds"] = budget.min_interval_seconds
        overrides[origin] = merged
    if overrides:
        values["origin_overrides"] = overrides
    for flag, key in (
            ("global_inflight", "global_inflight"),
            ("origin_inflight", "origin_inflight"),
            ("origin_min_interval", "origin_min_interval"),
            ("request_timeout", "request_timeout_seconds"),
            ("max_body_bytes", "max_body_bytes"),
            ("max_redirects", "max_redirects")):
        value = getattr(args, flag)
        if value is not None:
            values[key] = value
    return config_from_mapping(values)


def _emit_error(exc: Exception, code: int) -> int:
    payload = {"error": type(exc).__name__, "message": str(exc)}
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
    return code


def _run(args) -> int:
    try:
        config = _config_from_args(args)
        jobs = read_manifest(args.manifest)
    except (BatchConfigError, ManifestError) as exc:
        return _emit_error(exc, 2)

    runner = BatchRunner(jobs, config)
    interrupted = {"flag": False}

    def _handle_sigint(signum, frame):
        if not interrupted["flag"]:
            interrupted["flag"] = True
            runner.cancel()

    previous = signal.signal(signal.SIGINT, _handle_sigint)
    try:
        runner.run()
        try:
            with open(args.report, "w", encoding="utf-8") as fh:
                runner.write_report(fh)
        except OSError as exc:
            return _emit_error(
                BatchConfigError(
                    f"cannot write report {args.report!r}: {exc}"), 2)
        summary = runner.summary()
    finally:
        signal.signal(signal.SIGINT, previous)
        runner.close()

    print(json.dumps(summary, ensure_ascii=False))
    if interrupted["flag"]:
        return 130
    return 0 if summary["success"] else 3


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "run":
        return _run(args)
    raise SystemExit(2)


if __name__ == "__main__":
    sys.exit(main())
