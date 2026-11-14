"""Command line interface:

    python -m robots_admission --db DB update --url URL --fetched-at ISO \
        --status N --ttl SECONDS [--etag ETAG] [--body FILE | --body-text TEXT]
    python -m robots_admission --db DB check --url URL --agent TOKEN --now ISO
    python -m robots_admission --db DB batch --urls FILE --agent TOKEN \
        [--agent TOKEN ...] --now ISO
    python -m robots_admission --db DB origins
    python -m robots_admission --db DB failures [--origin URL]

Every command prints one JSON document on stdout.  Usage errors and
rejected parameters print a JSON error on stderr and exit 1.  No network
access is ever performed: ``update`` consumes robots bytes from a local
file or literal text only, and every instant is supplied by the caller.

``batch`` checks the same candidate URL list once per ``--agent``
identity, so a scheduler can compare two crawlers side by side.
"""

from __future__ import annotations

import argparse
import json
import sys

from .cache import RobotsCache, render_decision
from .errors import AdmissionError
from .validate import parse_iso_instant


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="robots_admission",
        description="Explainable robots.txt crawl admission (offline, SQLite)")
    p.add_argument("--db", required=True, help="SQLite cache file")
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("update", help="apply one fetch result to the cache")
    up.add_argument("--url", required=True,
                    help="any URL of the target origin")
    up.add_argument("--fetched-at", required=True,
                    help="timezone-aware ISO-8601 fetch instant")
    up.add_argument("--status", required=True, type=int,
                    help="HTTP status of the fetch (200/304/404/410/429/5xx)")
    up.add_argument("--ttl", required=True, type=int,
                    help="ttl_seconds (non-negative integer)")
    up.add_argument("--etag", help="validator of the representation")
    body = up.add_mutually_exclusive_group()
    body.add_argument("--body", help="local file with the raw robots bytes")
    body.add_argument("--body-text", help="literal robots text (UTF-8)")

    ck = sub.add_parser("check", help="admission decision for one URL")
    ck.add_argument("--url", required=True)
    ck.add_argument("--agent", required=True, help="crawler product token")
    ck.add_argument("--now", required=True,
                    help="timezone-aware ISO-8601 decision instant")
    ck.add_argument("--text", action="store_true",
                    help="also print the human-readable rendering")

    ba = sub.add_parser("batch", help="check a URL list as one or more agents")
    ba.add_argument("--urls", required=True,
                    help="UTF-8 file with one candidate URL per line "
                         "('#' comments and blank lines ignored)")
    ba.add_argument("--agent", required=True, action="append", dest="agents",
                    help="crawler product token (repeat for a second identity)")
    ba.add_argument("--now", required=True,
                    help="timezone-aware ISO-8601 decision instant")
    ba.add_argument("--text", action="store_true",
                    help="also print the human-readable rendering")

    sub.add_parser("origins", help="list origins with a cached policy")

    fa = sub.add_parser("failures", help="list recorded 429/5xx refresh failures")
    fa.add_argument("--origin", help="restrict to one origin URL")
    return p


def _read_urls(path: str) -> list[str]:
    urls = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                urls.append(stripped)
    return urls


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        with RobotsCache(args.db) as cache:
            if args.cmd == "update":
                body = None
                if args.body is not None:
                    with open(args.body, "rb") as fh:
                        body = fh.read()
                elif args.body_text is not None:
                    body = args.body_text.encode("utf-8")
                result = cache.update(
                    args.url,
                    fetched_at=parse_iso_instant(args.fetched_at, "fetched-at"),
                    status=args.status, ttl_seconds=args.ttl,
                    body=body, etag=args.etag)
                print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
                return 0

            if args.cmd == "check":
                decision = cache.check(
                    args.url, args.agent,
                    parse_iso_instant(args.now, "now"))
                if args.text:
                    print(render_decision(decision))
                print(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2))
                return 0

            if args.cmd == "batch":
                now = parse_iso_instant(args.now, "now")
                urls = _read_urls(args.urls)
                results = []
                summary = {a: {"allow": 0, "disallow": 0,
                               "needs_refresh": 0, "error": 0}
                           for a in args.agents}
                for url in urls:
                    outcomes = {}
                    for agent in args.agents:
                        try:
                            decision = cache.check(url, agent, now)
                        except AdmissionError as exc:
                            summary[agent]["error"] += 1
                            outcomes[agent] = {"error": str(exc)}
                            continue
                        summary[agent][decision.outcome] += 1
                        outcomes[agent] = decision.to_dict()
                        if args.text:
                            print(render_decision(decision))
                    results.append({"url": url, "outcomes": outcomes})
                print(json.dumps({"now": now.isoformat(), "agents": args.agents,
                                  "results": results, "summary": summary},
                                 ensure_ascii=False, indent=2))
                return 0

            if args.cmd == "origins":
                print(json.dumps({"origins": cache.origins()},
                                 ensure_ascii=False, indent=2))
                return 0

            if args.cmd == "failures":
                print(json.dumps(
                    {"failures": cache.failures(args.origin)},
                    ensure_ascii=False, indent=2))
                return 0
    except AdmissionError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
