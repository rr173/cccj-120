"""Command line interface:  python -m archivestore --storage S --key KID=PATH ...

Every command prints one JSON result line on stdout.  Errors print a single
JSON error line on stderr and exit with status 1; key material is never
printed.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from .errors import ArchiveStoreError
from .store import ArchiveStore


def _parse_key(spec: str):
    if "=" not in spec:
        raise argparse.ArgumentTypeError("expected KEY_ID=PATH")
    kid, path = spec.split("=", 1)
    if not kid:
        raise argparse.ArgumentTypeError("empty key_id")
    return kid, path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="archivestore")
    p.add_argument("--storage", required=True, help="storage directory")
    p.add_argument("--key", action="append", type=_parse_key, default=[],
                   metavar="KEY_ID=PATH",
                   help="map a key_id to its key file (repeatable)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seal", help="encrypt a file into a new archive")
    s.add_argument("--source", required=True)
    s.add_argument("--key-id", required=True)
    s.add_argument("--block-size", type=int, required=True)

    r = sub.add_parser("read-range", help="read [start, end) into a new file")
    r.add_argument("--archive", required=True)
    r.add_argument("--start", type=int, required=True)
    r.add_argument("--end", type=int, required=True)
    r.add_argument("--out", required=True)

    v = sub.add_parser("verify", help="verify a whole archive")
    v.add_argument("--archive", required=True)

    t = sub.add_parser("rotate", help="re-wrap all DEKs to a new KEK")
    t.add_argument("--old", required=True)
    t.add_argument("--new", required=True)

    sub.add_parser("list", help="list archive ids")
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    keys = dict(args.key)
    try:
        with ArchiveStore(args.storage, keys) as store:
            if args.cmd == "seal":
                aid = store.seal(args.source, key_id=args.key_id,
                                 block_size=args.block_size)
                result = {"archive_id": aid}
            elif args.cmd == "read-range":
                stats = store.read_range(args.archive, args.start, args.end, args.out)
                result = dataclasses.asdict(stats)
            elif args.cmd == "verify":
                stats = store.verify(args.archive)
                result = dataclasses.asdict(stats)
            elif args.cmd == "rotate":
                result = {"rotated": store.rotate(old_key_id=args.old,
                                                  new_key_id=args.new)}
            elif args.cmd == "list":
                result = {"archives": store.list_archives(),
                          "envelopes": store.envelope_key_ids()}
            else:  # pragma: no cover
                raise AssertionError(args.cmd)
    except (ArchiveStoreError, OSError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}),
              file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "command": args.cmd, **result}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
