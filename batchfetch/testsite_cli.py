"""Run the scripted two-port test site as a standalone process.

    python -m batchfetch.testsite --port-a 8081 --port-b 8082 \
        [--port-file FILE]

Writes the chosen ports as JSON to ``--port-file`` (or prints them on
stdout) and serves until SIGINT or a ``/__control/shutdown`` call.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time

from .testsite import DualPortSite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="batchfetch.testsite",
        description="Controllable two-port localhost HTTP test site.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port-a", type=int, default=0)
    parser.add_argument("--port-b", type=int, default=0)
    parser.add_argument("--port-file", default=None)
    args = parser.parse_args(argv)

    site = DualPortSite(host=args.host, port_a=args.port_a,
                        port_b=args.port_b).start()
    info = {"host": args.host, "port_a": site.port_a,
            "port_b": site.port_b,
            "origin_a": site.origin_a, "origin_b": site.origin_b}
    if args.port_file:
        with open(args.port_file, "w", encoding="utf-8") as fh:
            json.dump(info, fh)
    else:
        print(json.dumps(info))

    stop = {"flag": False}

    def _handle(signum, frame):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    try:
        while not stop["flag"]:
            time.sleep(0.1)
    finally:
        site.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
