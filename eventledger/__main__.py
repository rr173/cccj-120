"""Command line interface:

    python -m eventledger --ledger L.db import FILE [FILE ...]
    python -m eventledger --ledger L.db runs
    python -m eventledger --ledger L.db timeline --run RUN --request REQ
    python -m eventledger --ledger L.db summary --run RUN
    python -m eventledger --ledger L.db export --out PATH [--run RUN]
    python -m eventledger --ledger L.db stats [--run RUN]

Every successful command prints one JSON result on stdout.  A rejected
batch prints a JSON object with a ``problems`` array (each with run_id,
request_id, event_id, line and reason) on stderr and exits 1; the
ledger is left untouched by the failed batch.  No network access and no
archive/WARC files are ever touched.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .errors import EventLedgerError, ValidationError
from .ledger import Ledger


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eventledger",
                                description="Offline crawler run-event ledger")
    p.add_argument("--ledger", required=True, help="SQLite ledger file")
    sub = p.add_subparsers(dest="cmd", required=True)

    imp = sub.add_parser("import", help="validate and atomically import "
                                        "one or more JSONL batch files")
    imp.add_argument("files", nargs="+", help="UTF-8 JSONL event files")

    sub.add_parser("runs", help="list run_ids in the ledger")

    tl = sub.add_parser("timeline", help="reconstructed timeline of one "
                                         "request")
    tl.add_argument("--run", required=True)
    tl.add_argument("--request", required=True)

    sm = sub.add_parser("summary", help="aggregate counters for one run")
    sm.add_argument("--run", required=True)

    ex = sub.add_parser("export", help="stable, re-importable JSONL export")
    ex.add_argument("--out", required=True)
    ex.add_argument("--run", help="limit export to one run_id")

    st = sub.add_parser("stats", help="event counters")
    st.add_argument("--run")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        with Ledger(args.ledger) as ledger:
            result: Any
            if args.cmd == "import":
                result = {"imports": []}
                for path in args.files:
                    r = ledger.import_file(path)
                    result["imports"].append(r.to_dict())
            elif args.cmd == "runs":
                result = {"runs": ledger.list_runs()}
            elif args.cmd == "timeline":
                result = ledger.request_timeline(args.run, args.request).to_dict()
            elif args.cmd == "summary":
                result = ledger.run_summary(args.run).to_dict()
            elif args.cmd == "export":
                n = ledger.export_jsonl(args.out, run_id=args.run)
                result = {"exported_events": n, "out": args.out,
                          "run_id": args.run}
            elif args.cmd == "stats":
                result = {"run_id": args.run,
                          "events": ledger.event_count(args.run)}
            else:  # pragma: no cover - argparse enforces choices
                raise SystemExit(2)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except ValidationError as exc:
        payload = {"error": type(exc).__name__,
                   "problems": [p.to_dict() for p in exc.problems]}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True),
              file=sys.stderr)
        return 1
    except EventLedgerError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)},
                         ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(json.dumps({"error": "FileNotFoundError", "message": str(exc)},
                         ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
