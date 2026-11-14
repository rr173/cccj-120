"""Exception hierarchy for sitemapdiscover.

Two families of errors exist:

* configuration errors (bad roots, an unusable mapping file, duplicate
  ``source_url`` entries) abort the whole run before discovery starts;
* per-map rejections never abort the run: they are recorded as failure
  nodes, the map's entries are discarded as a whole, and independent
  branches continue.

A :class:`MapRejection` is normally raised by the streaming reader and
turned into a failure node by the engine; callers mostly see it only
through the report.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class SitemapDiscoverError(Exception):
    """Base class for every sitemapdiscover error."""


class ConfigurationError(SitemapDiscoverError):
    """The roots or the mapping file cannot be used for this run."""


class MappingConflictError(ConfigurationError):
    """Two mapping entries normalize to the same ``source_url``.

    The two positions and raw URLs are kept so the operator can fix the
    mapping file; no traversal is attempted with an ambiguous mapping.
    """

    def __init__(self, message: str, *, first_index=None, first_url=None,
                 second_index=None, second_url=None):
        super().__init__(message)
        self.first_index = first_index
        self.first_url = first_url
        self.second_index = second_index
        self.second_url = second_url


class URLRuleError(SitemapDiscoverError, ValueError):
    """A URL violates the shared absolute-http(s) URL rules.

    Covers: non-http(s) scheme, username/password, missing host, invalid
    port, a fragment, and ASCII whitespace/control characters.  Path and
    query are otherwise preserved byte for byte, so an encoded slash is
    never merged with a real slash.
    """


@dataclass(frozen=True)
class RejectionDetail:
    """Location information attached to a rejected map."""

    kind: str
    reason: str
    entry_index: Optional[int] = None
    line: Optional[int] = None
    column: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "entry_index": self.entry_index,
            "line": self.line,
            "column": self.column,
        }


class MapRejection(SitemapDiscoverError):
    """One map is rejected as a whole (all-or-nothing).

    ``kind`` is a stable machine-readable category (``invalid_xml``,
    ``gzip_crc``, ``cross_origin``, ``limit_entries``, ...); ``reason``
    is the concrete human-readable cause.  ``entry_index`` is the 1-based
    url/sitemap entry number when the problem is tied to an entry.
    """

    def __init__(self, kind: str, reason: str, *, entry_index=None,
                 line=None, column=None):
        super().__init__(reason)
        self.detail = RejectionDetail(kind, reason,
                                      entry_index=entry_index,
                                      line=line, column=column)
        self.kind = kind
        self.reason = reason


class LimitsError(ConfigurationError):
    """A limit value is illegal (e.g. zero or negative where positive is
    required)."""
