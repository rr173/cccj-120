#!/usr/bin/env python3
"""Subprocess worker for the demo harness.

Each invocation is a fresh OS process (as required by the drill).  It prints
exactly one machine-readable line on stdout:

    RESULT {...}   on success (includes memory stats)
    ERROR  {...}   on failure (exit status 1)

Key material is never printed.  Extra commands beyond the public CLI:
  rotate --pause-stage/--pause-dir   block at a defined point until released
  hold-read                          open a handle, signal, wait, then read
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from archivestore import ArchiveStore  # noqa: E402
from archivestore.errors import ArchiveStoreError  # noqa: E402
from archivestore.store import STAGE_POST_COMMIT, STAGE_PRE_COMMIT  # noqa: E402


def _parse_key(spec: str):
    kid, _, path = spec.partition("=")
    if not kid or not path:
        raise argparse.ArgumentTypeError("expected KEY_ID=PATH")
    return kid, path


def _parse_range(spec: str):
    # START:END:OUT
    a, _, rest = spec.partition(":")
    b, _, out = rest.partition(":")
    return int(a), int(b), out


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
    p = argparse.ArgumentParser(prog="worker")
    p.add_argument("--storage", required=True)
    p.add_argument("--key", action="append", type=_parse_key, default=[])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seal")
    s.add_argument("--source", required=True)
    s.add_argument("--key-id", required=True)
    s.add_argument("--block-size", type=int, required=True)

    r = sub.add_parser("read")
    r.add_argument("--archive", required=True)
    r.add_argument("--start", type=int, required=True)
    r.add_argument("--end", type=int, required=True)
    r.add_argument("--out", required=True)

    v = sub.add_parser("verify")
    v.add_argument("--archive", required=True)

    t = sub.add_parser("rotate")
    t.add_argument("--old", required=True)
    t.add_argument("--new", required=True)
    t.add_argument("--pause-stage", choices=[STAGE_PRE_COMMIT, STAGE_POST_COMMIT])
    t.add_argument("--pause-dir")

    h = sub.add_parser("hold-read")
    h.add_argument("--archive", required=True)
    h.add_argument("--sentinel", required=True, help="touch after the handle is open")
    h.add_argument("--go", required=True, help="wait for this file, then read")
    h.add_argument("--range", action="append", type=_parse_range, default=[],
                   metavar="START:END:OUT")
    return p


def main(argv=None) -> int:
    tracemalloc.start()
    args = build_parser().parse_args(argv)
    keys = dict(args.key)
    try:
        if args.cmd == "hold-read":
            result = _hold_read(args, keys)
        else:
            with ArchiveStore(args.storage, keys) as store:
                if args.cmd == "seal":
                    result = {"archive_id": store.seal(
                        args.source, key_id=args.key_id,
                        block_size=args.block_size)}
                elif args.cmd == "read":
                    stats = store.read_range(args.archive, args.start, args.end,
                                             args.out)
                    result = dict(stats._asdict()) if hasattr(stats, "_asdict") else {
                        "blocks_read": stats.blocks_read,
                        "cipher_bytes_read": stats.cipher_bytes_read,
                        "bytes_written": stats.bytes_written,
                    }
                elif args.cmd == "verify":
                    stats = store.verify(args.archive)
                    result = {
                        "blocks_checked": stats.blocks_checked,
                        "bytes_checked": stats.bytes_checked,
                        "sha256_hex": stats.sha256_hex,
                    }
                elif args.cmd == "rotate":
                    hook = _make_pause_hook(args) if args.pause_stage else None
                    result = {"rotated": store.rotate(
                        old_key_id=args.old, new_key_id=args.new, pause_hook=hook)}
                else:  # pragma: no cover
                    raise AssertionError(args.cmd)
    except (ArchiveStoreError, TimeoutError, ValueError, OSError) as exc:
        print("ERROR " + json.dumps({"type": type(exc).__name__,
                                     "message": str(exc)}))
        return 1
    print("RESULT " + json.dumps({**result, "mem": _memstats()}))
    return 0


def _make_pause_hook(args):
    pause_dir = Path(args.pause_dir)

    def hook(stage: str) -> None:
        if stage != args.pause_stage:
            return
        (pause_dir / f"{stage}.paused").write_text(str(os.getpid()))
        _wait_for(pause_dir / f"{stage}.release")

    return hook


def _hold_read(args, keys) -> dict:
    """Open a handle (unwrapping the DEK now), signal, wait, then read with
    the DEK already held in memory -- even if a rotation committed meanwhile."""
    with ArchiveStore(args.storage, keys) as store:
        handle = store.open(args.archive)
        Path(args.sentinel).write_text("open")
        _wait_for(Path(args.go))
        reads = []
        for start, end, out in args.range:
            stats = handle.read_range(start, end, out)
            digest = hashlib.sha256(Path(out).read_bytes()).hexdigest()
            reads.append({
                "start": start, "end": end, "out": out,
                "blocks_read": stats.blocks_read,
                "cipher_bytes_read": stats.cipher_bytes_read,
                "bytes_written": stats.bytes_written,
                "sha256": digest,
            })
        handle.close()
    return {"reads": reads}


if __name__ == "__main__":
    sys.exit(main())
