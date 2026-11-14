#!/usr/bin/env python3
"""Subprocess worker for the warcdb drill.

Each invocation is a fresh OS process (as required by the drill).  It
prints exactly one machine-readable line on stdout:

    RESULT {...}   on success (includes memory stats)
    ERROR  {...}   on failure (exit status 1)

Key material is never printed.  Extra options beyond the public CLI:
  import --pause-stage pre_commit --pause-dir D   block inside the import
                                                  transaction until released
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sqlite3
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from archivestore import ArchiveStore  # noqa: E402
from archivestore.errors import ArchiveStoreError  # noqa: E402
from warcdb import FactDB, FactStore, Limits  # noqa: E402
from warcdb.errors import WarcDbError  # noqa: E402
from warcdb.warc import parse_instant  # noqa: E402


def _parse_key(spec: str):
    kid, _, path = spec.partition("=")
    if not kid or not path:
        raise argparse.ArgumentTypeError("expected KEY_ID=PATH")
    return kid, path


def _wait_for(path: Path, timeout: float = 180.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for {path}")


def _memstats():
    peak_py = tracemalloc.get_traced_memory()[1] if tracemalloc.is_tracing() else 0
    return {
        "max_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "py_traced_peak_bytes": peak_py,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="warc-worker")
    p.add_argument("--storage")
    p.add_argument("--facts", required=True)
    p.add_argument("--key", action="append", type=_parse_key, default=[])
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("import")
    i.add_argument("--archive", required=True)
    i.add_argument("--read-chunk", type=int, default=Limits.read_chunk)
    i.add_argument("--max-header-bytes", type=int, default=Limits.max_header_bytes)
    i.add_argument("--max-block-bytes", type=int, default=Limits.max_block_bytes)
    i.add_argument("--pause-stage", choices=["pre_commit"])
    i.add_argument("--pause-dir")

    c = sub.add_parser("chain")
    c.add_argument("--record-id", required=True)

    g = sub.add_parser("get")
    g.add_argument("--record-id", required=True)

    l = sub.add_parser("lookup")
    l.add_argument("--uri", required=True)
    l.add_argument("--at", required=True)

    e = sub.add_parser("export")
    e.add_argument("--record-id", required=True)
    e.add_argument("--out", required=True)

    f = sub.add_parser("fetches")
    f.add_argument("--uri")

    sub.add_parser("stats")
    sub.add_parser("dump")
    return p


def main(argv=None) -> int:
    tracemalloc.start()
    args = build_parser().parse_args(argv)
    keys = dict(args.key)
    try:
        with FactDB(args.facts) as db:
            if args.cmd == "import":
                limits = Limits(read_chunk=args.read_chunk,
                                max_header_bytes=args.max_header_bytes,
                                max_block_bytes=args.max_block_bytes)
                hook = _make_pause_hook(args) if args.pause_stage else None
                with ArchiveStore(args.storage, keys) as store:
                    result = FactStore(store, db).import_archive(
                        args.archive, limits=limits, pause_hook=hook)
                payload = result.to_dict()
            elif args.cmd == "export":
                with ArchiveStore(args.storage, keys) as store:
                    result = FactStore(store, db).export_body(
                        args.record_id, args.out)
                payload = result.to_dict()
            elif args.cmd == "chain":
                payload = db.chain(args.record_id)
            elif args.cmd == "get":
                fetch = db.get(args.record_id)
                if fetch is None:
                    from warcdb.errors import RecordNotFoundError
                    raise RecordNotFoundError(
                        f"no fetch with Record-ID {args.record_id!r}")
                payload = fetch.to_dict()
            elif args.cmd == "lookup":
                _, cutoff_us = parse_instant(args.at, what="--at")
                payload = db.lookup(args.uri, cutoff_us)
            elif args.cmd == "fetches":
                payload = {"fetches": db.list_fetches(args.uri)}
            elif args.cmd == "stats":
                payload = db.stats()
            elif args.cmd == "dump":
                payload = db.dump()
            else:  # pragma: no cover
                raise AssertionError(args.cmd)
    except (WarcDbError, ArchiveStoreError, TimeoutError, ValueError, OSError,
            sqlite3.Error) as exc:
        print("ERROR " + json.dumps({"type": type(exc).__name__,
                                     "message": str(exc)}))
        return 1
    print("RESULT " + json.dumps({**payload, "mem": _memstats()}))
    return 0


def _make_pause_hook(args):
    pause_dir = Path(args.pause_dir)

    def hook(stage: str) -> None:
        if stage != args.pause_stage:
            return
        (pause_dir / f"{stage}.paused").write_text(str(os.getpid()))
        _wait_for(pause_dir / f"{stage}.release")

    return hook


if __name__ == "__main__":
    sys.exit(main())
