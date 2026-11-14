"""Command line interface.

    python -m sitemapdiscover discover --roots ROOTS --mapping FILE \
        --report OUT [--format jsonl|json] [--no-sources] \
        [--max-file-bytes N] [--max-decompressed-bytes N] \
        [--max-entries-per-map N] [--max-maps N] [--max-depth N] \
        [--max-loc-length N] [--cache-dir DIR]

``--roots`` points to a UTF-8 text file with one root sitemap URL per
line (blank lines and ``#`` comments ignored); it may be repeated, and
roots are processed in file order, then line order.

The command streams the report to ``--report`` and prints a one-line
JSON summary on stdout.  Exit code is 0 only when the discovery is
complete; any failed map, cycle or limit stop makes ``complete=false``
and exits 3, with every failure listed in the report.  Configuration
problems (bad roots, bad mapping, duplicate mapping keys) exit 2 with a
JSON error on stderr and no report file.

    python -m sitemapdiscover pages --report report.jsonl   # replay a report
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from .engine import Discovery
from .errors import (ConfigurationError, LimitsError, MappingConflictError,
                     SitemapDiscoverError)
from .mapping import DownloadMapping
from .profile import Limits


def _read_roots(paths: List[str]) -> List[str]:
    roots: List[str] = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, start=1):
                    text = line.strip()
                    if text and not text.startswith("#"):
                        roots.append(text)
        except OSError as exc:
            raise ConfigurationError(
                f"cannot read roots file {path!r}: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise ConfigurationError(
                f"roots file {path!r} is not UTF-8: {exc}") from exc
    if not roots:
        raise ConfigurationError("no root URLs were given")
    return roots


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sitemapdiscover",
        description="Offline sitemap discovery: walk root sitemaps through "
                    "their indexes to the real pages and emit a "
                    "deduplicated, source-annotated fetch list.  No network "
                    "access, no robots evaluation, no page parsing.")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="run a discovery")
    d.add_argument("--roots", required=True, action="append",
                   metavar="FILE",
                   help="UTF-8 file with one root sitemap URL per line "
                        "(repeatable; roots keep file/line order)")
    d.add_argument("--mapping", required=True, metavar="FILE",
                   help="JSON mapping of source_url -> local downloaded file")
    d.add_argument("--report", required=True, metavar="FILE",
                   help="report output path (rewritten, never appended)")
    d.add_argument("--format", choices=("jsonl", "json"), default="jsonl",
                   help="jsonl: one JSON object per line (default); "
                        "json: one JSON document")
    d.add_argument("--no-sources", action="store_true",
                   help="omit per-page source positions from the report")
    d.add_argument("--max-file-bytes", type=int, default=None,
                   help="compressed bytes allowed per map file")
    d.add_argument("--max-decompressed-bytes", type=int, default=None,
                   help="decompressed bytes allowed per map file")
    d.add_argument("--max-entries-per-map", type=int, default=None,
                   help="url/sitemap entries allowed per map")
    d.add_argument("--max-maps", type=int, default=None,
                   help="distinct map files allowed in this run")
    d.add_argument("--max-depth", type=int, default=None,
                   help="maximum reference depth (roots are depth 0)")
    d.add_argument("--max-loc-length", type=int, default=None,
                   help="maximum characters in one trimmed <loc>")
    d.add_argument("--cache-dir", default=None, metavar="DIR",
                   help="optional scratch directory for the run-local SQLite "
                        "file (default: a fresh temporary directory)")

    pg = sub.add_parser("pages", help="print the page list from an existing "
                                      "JSONL report (streaming replay)")
    pg.add_argument("--report", required=True)
    return p


def _limits_from_args(args) -> Limits:
    values = {}
    for name, attr in (
            ("max_file_bytes", "max_file_bytes"),
            ("max_decompressed_bytes", "max_decompressed_bytes"),
            ("max_entries_per_map", "max_entries_per_map"),
            ("max_maps", "max_maps"),
            ("max_depth", "max_depth"),
            ("max_loc_length", "max_loc_length")):
        value = getattr(args, attr)
        if value is not None:
            values[name] = value
    return Limits.from_mapping(values)


def _emit_error(exc: Exception, code: int) -> int:
    payload = {"error": type(exc).__name__, "message": str(exc)}
    if isinstance(exc, MappingConflictError):
        payload["conflict"] = {
            "first_index": exc.first_index, "first_url": exc.first_url,
            "second_index": exc.second_index, "second_url": exc.second_url,
        }
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
    return code


def _discover(args) -> int:
    try:
        limits = _limits_from_args(args)
        roots = _read_roots(args.roots)
        mapping = DownloadMapping.from_file(args.mapping)
        discovery = Discovery(roots, mapping, limits,
                              scratch_dir=args.cache_dir)
    except (ConfigurationError, LimitsError) as exc:
        return _emit_error(exc, 2)
    try:
        discovery.run()
        try:
            report = discovery.write_report(
                args.report, format=args.format,
                include_sources=not args.no_sources)
        except OSError as exc:
            return _emit_error(
                ConfigurationError(
                    f"cannot write report {args.report!r}: {exc}"), 2)
    finally:
        discovery.close()
    summary = {
        "complete": report["complete"],
        "report": args.report,
        "counts": report["counts"],
        "failures": report["counts"]["map_failures"]
                    + report["counts"]["issue_failures"],
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if report["complete"] else 3


def _pages(args) -> int:
    # Streaming replay: never loads the report file into memory.
    try:
        fh = open(args.report, "r", encoding="utf-8")
    except OSError as exc:
        return _emit_error(
            ConfigurationError(f"cannot read report {args.report!r}: {exc}"), 2)
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                return _emit_error(
                    ConfigurationError(
                        f"report line is not JSON: {exc}"), 2)
            if obj.get("type") == "page":
                print(json.dumps(obj, ensure_ascii=False))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.cmd == "discover":
            return _discover(args)
        if args.cmd == "pages":
            return _pages(args)
        raise SystemExit(2)
    except SitemapDiscoverError as exc:
        return _emit_error(exc, 1)


if __name__ == "__main__":
    sys.exit(main())
