"""Helper for the forced-exit drill (runs in its own process).

Opens the ledger, starts a batch import, and from the pre-commit hook
sends SIGKILL to itself *after* every validation passed but before
COMMIT.  The parent test then proves the SQLite file holds no rows from
the killed batch and can be reopened and re-imported cleanly.

Run with -I so the untrusted input directory cannot shadow stdlib
modules; the script itself lives in the tests directory.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eventledger import Ledger  # noqa: E402


def main() -> int:
    db_path, jsonl_path = sys.argv[1], sys.argv[2]
    ledger = Ledger(db_path)

    def kill_self() -> None:
        os.kill(os.getpid(), signal.SIGKILL)

    ledger.import_file(jsonl_path, pre_commit_hook=kill_self)
    return 0  # unreachable


if __name__ == "__main__":
    raise SystemExit(main())
