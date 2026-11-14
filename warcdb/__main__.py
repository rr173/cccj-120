"""Command line interface:

    python -m warcdb --facts F.db [--storage S] [--key KID=PATH ...] CMD ...

Commands:
    import  --archive HEX [--read-chunk N] [--max-header-bytes N]
            [--max-block-bytes N]        (needs --storage and keys)
    chain   --record-id RID
    get     --record-id RID
    lookup  --uri URI --at ISO8601-WITH-TZ
    export  --record-id RID --out PATH   (needs --storage and keys)
    fetches [--uri URI]
    stats
    dump                                (deterministic full dump)

Every command prints one JSON result line on stdout.  Errors print a
single JSON error line on stderr and exit with status 1.  Query commands
touch only the fact DB; import/export also open the archive storage.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from archivestore.errors import ArchiveStoreError
from archivestore.store import ArchiveStore

from .core import FactStore
from .db import FactDB
from .errors import WarcDbError
from .warc import Limits, parse_instant


def _parse_key(spec: str):
    if "=" not in spec:
        raise argparse.ArgumentTypeError("expected KEY_ID=PATH")
    kid, path = spec.split("=", 1)
    if not kid:
        raise argparse.ArgumentTypeError("empty key_id")
    return kid, path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="warcdb")
    p.add_argument("--facts", required=True, help="fact-store SQLite file")
    p.add_argument("--storage", help="archivestore storage directory "
                                     "(required for import/export)")
    p.add_argument("--key", action="append", type=_parse_key, default=[],
                   metavar="KEY_ID=PATH",
                   help="map a key_id to its key file (repeatable)")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("import", help="import all records of one archive")
    i.add_argument("--archive", required=True, help="archive_id hex")
    i.add_argument("--read-chunk", type=int, default=Limits.read_chunk)
    i.add_argument("--max-header-bytes", type=int, default=Limits.max_header_bytes)
    i.add_argument("--max-block-bytes", type=int, default=Limits.max_block_bytes)

    c = sub.add_parser("chain", help="full reference chain of a Record-ID")
    c.add_argument("--record-id", required=True)

    g = sub.add_parser("get", help="one fetch with its HTTP headers")
    g.add_argument("--record-id", required=True)

    l = sub.add_parser("lookup", help="latest fetch of a URI at/before an instant")
    l.add_argument("--uri", required=True)
    l.add_argument("--at", required=True,
                   help="ISO-8601 instant, timezone required")

    e = sub.add_parser("export", help="export the resolved body of a fetch")
    e.add_argument("--record-id", required=True)
    e.add_argument("--out", required=True)

    f = sub.add_parser("fetches", help="list fetches, optionally filtered by URI")
    f.add_argument("--uri")

    sub.add_parser("stats", help="fact-store counters")
    sub.add_parser("dump", help="deterministic full dump")
    return p


def _need_storage(args) -> None:
    if not args.storage:
        raise WarcDbError(f"command {args.cmd!r} needs --storage")


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        with FactDB(args.facts) as db:
            if args.cmd == "import":
                _need_storage(args)
                limits = Limits(read_chunk=args.read_chunk,
                                max_header_bytes=args.max_header_bytes,
                                max_block_bytes=args.max_block_bytes)
                with ArchiveStore(args.storage, dict(args.key)) as store:
                    result = FactStore(store, db).import_archive(
                        args.archive, limits=limits)
                payload = result.to_dict()
            elif args.cmd == "export":
                _need_storage(args)
                with ArchiveStore(args.storage, dict(args.key)) as store:
                    result = FactStore(store, db).export_body(
                        args.record_id, args.out)
                payload = result.to_dict()
            elif args.cmd == "chain":
                payload = db.chain(args.record_id)
            elif args.cmd == "get":
                fetch = db.get(args.record_id)
                if fetch is None:
                    from .errors import RecordNotFoundError
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
    except (WarcDbError, ArchiveStoreError, sqlite3.Error, OSError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}),
              file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "command": args.cmd, **payload}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
