"""Command line interface:

    python -m tableextract FILE --table-id ID [--header-rows N]

Reads the local UTF-8 HTML FILE (at most 4 MiB), extracts the <table>
whose id is ID (unique in the whole document), and prints the full
extraction as one JSON document on stdout.  Failures print a JSON error
on stderr and exit 1; nothing is ever written to stdout on failure, so
a half result can never be mistaken for success data.
"""

from __future__ import annotations

import argparse
import json
import sys

from .errors import TableExtractError
from .extractor import extract_table


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tableextract",
        description="Offline HTML table extractor: restore one explicitly "
                    "closed <table> from a local UTF-8 HTML file into "
                    "rectangular JSON (standard library only, no network).")
    p.add_argument("file", help="local UTF-8 HTML file (at most 4 MiB)")
    p.add_argument("--table-id", required=True,
                   help="id of the target <table>; must occur on exactly one "
                        "table element in the whole document")
    p.add_argument("--header-rows", type=int, default=0, metavar="N",
                   help="how many of the first rows (document order) are "
                        "column headers; 0 (default) means no header")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.header_rows < 0:
        print(json.dumps(
            {"error": "--header-rows 必须是非负整数"},
            ensure_ascii=False), file=sys.stderr)
        return 1
    try:
        with open(args.file, "rb") as fh:
            html_bytes = fh.read()
    except OSError as exc:
        print(json.dumps(
            {"error": f"无法读取 {args.file}: {exc}"},
            ensure_ascii=False), file=sys.stderr)
        return 1
    try:
        result = extract_table(html_bytes, args.table_id, args.header_rows)
    except TableExtractError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
