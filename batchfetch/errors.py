"""Exception hierarchy for batchfetch.

Two families:

* :class:`BatchConfigError` -- the manifest or the configuration cannot be
  used at all.  Validation happens before any socket is opened, so a rejected
  batch never touches the network.
* :class:`BatchRunError` -- runtime misuse of the API (e.g. running one
  :class:`~batchfetch.scheduler.BatchRunner` twice).  Per-job fetch failures
  are **not** exceptions: they are terminal ``failed`` results.
"""

from __future__ import annotations


class BatchFetchError(Exception):
    """Base class for every batchfetch error."""


class BatchConfigError(BatchFetchError, ValueError):
    """The manifest or configuration is invalid (rejected before network)."""


class ManifestError(BatchConfigError):
    """The JSONL manifest cannot be used (bad line, bad job_id, dup, ...)."""

    def __init__(self, message: str, *, line: int | None = None):
        super().__init__(message)
        self.line = line


class BatchRunError(BatchFetchError):
    """The runner itself was misused (for example ``run()`` called twice)."""
