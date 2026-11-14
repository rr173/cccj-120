"""Exception hierarchy for robots_admission.

Every rejected call raises (or, on the CLI, reports) a diagnostic before
any write happens, so a bad URL or a bad parameter can never modify the
cached policy of an origin.
"""

from __future__ import annotations


class AdmissionError(Exception):
    """Base class for all robots_admission errors."""


class InvalidURLError(AdmissionError, ValueError):
    """A URL cannot enter the admission pipeline.

    Covers: unsupported scheme (only http/https), username/password
    userinfo, missing host, invalid port, and malformed or non-hex
    percent escapes in path/query.
    """


class InvalidParameterError(AdmissionError, ValueError):
    """A call parameter has an illegal value.

    Covers: naive (timezone-less) datetimes, empty/non-ASCII/whitespace
    product tokens, negative ttl_seconds, and similar contract
    violations.  Raised before any write: the cache is never modified
    by a rejected call.
    """
