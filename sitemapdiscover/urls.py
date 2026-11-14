"""The one URL rule shared by roots, map contents and the local mapping.

Identity normalization
----------------------
The *identity key* (used to look a URL up in the local mapping and to
merge duplicate pages) is::

    <lowercase scheme>://<IDNA/lowercase host>[:<non-default port>]
    <path exactly as written, defaulting to "/">[?<query exactly as written>]

* scheme and host name are lowercased; Unicode hosts are IDNA-encoded;
  IPv6 literals are canonically compressed;
* http:80 and https:443 are the default ports and are omitted from the
  key; other ports are kept;
* path and query are **not** touched: ``%2F`` and ``/`` are different
  pages, ``%7E`` and ``~`` are different pages, dot segments are not
  resolved and repeated slashes are not collapsed.

The *display URL* stored in the report is the same string with only the
scheme/host/port canonicalized (host lowercased and IDNA-encoded, default
port removed, IPv6 bracket form compressed); path and query keep the
caller's exact spelling.

Rejections (never silently repaired):
    non-http(s) scheme, username/password, missing host, invalid port,
    any fragment (even an empty ``#``), ASCII whitespace or control
    characters anywhere, and malformed ``%`` escapes in path/query.

Fragments are detected on the *raw* string: ``http://h/p#`` is a
fragment too, and ``#`` inside a query is a fragment separator rather
than query data.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from .errors import URLRuleError

_DEFAULT_PORT = {"http": 80, "https": 443}
_FORBIDDEN_CHARS = frozenset(
    "\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\x0c\r\x0e\x0f"
    "\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f"
    " \x7f")
_HEX = frozenset("0123456789abcdefABCDEF")


def _check_percent(text: str, what: str, url: str) -> None:
    i, n = 0, len(text)
    while i < n:
        if text[i] == "%":
            if i + 2 < n and text[i + 1] in _HEX and text[i + 2] in _HEX:
                i += 3
                continue
            raise URLRuleError(
                f"malformed percent escape at offset {i} of {what} in {url!r}")
        i += 1


def _normalize_host(hostname: str, url: str) -> str:
    host = hostname.lower()
    if not host:
        raise URLRuleError(f"URL {url!r} has an empty host")
    if ":" in host:  # IPv6 literal (brackets already removed by urlsplit)
        try:
            return ipaddress.IPv6Address(host).compressed
        except ValueError as exc:
            raise URLRuleError(f"invalid IPv6 host {hostname!r} in {url!r}") from exc
    try:
        return host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError) as exc:
        raise URLRuleError(f"invalid host name {hostname!r} in {url!r}") from exc


@dataclass(frozen=True)
class ParsedURL:
    """A validated, canonicalized absolute HTTP/HTTPS URL."""

    scheme: str
    host: str            # lowercase / IDNA ASCII / compressed IPv6
    port: Optional[int]  # None means the scheme default
    path: str            # exact spelling, defaults to "/"
    query: str           # exact spelling ("" means no query)
    original: str        # the raw input string (after the head/tail trim
                         # performed by the caller for loc text)

    @property
    def origin(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        if self.port is None:
            return f"{self.scheme}://{host}"
        return f"{self.scheme}://{host}:{self.port}"

    @property
    def identity(self) -> str:
        """The canonical merge/lookup key (path and query byte-exact)."""
        return self.origin + self.path + ("?" + self.query if self.query else "")

    @property
    def has_query(self) -> bool:
        return bool(self.query)

    def display(self) -> str:
        """Canonical scheme/host/port + the caller's exact path and query."""
        return self.identity

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.display()


def parse_url(raw: str) -> ParsedURL:
    """Validate ``raw`` and return its parsed/canonical form.

    Raises :class:`URLRuleError` for anything outside the rule above.
    """
    if not isinstance(raw, str):
        raise TypeError(f"url must be str, got {type(raw).__name__}")
    for pos, ch in enumerate(raw):
        if ch in _FORBIDDEN_CHARS:
            name = "space" if ch == " " else f"control character U+{ord(ch):04X}"
            raise URLRuleError(f"URL contains {name} at offset {pos}: {raw!r}")
    # urlsplit() strips ASCII tab/newline and leading C0/space, and it
    # rejects some malformed values; do our own checks on the raw string
    # first so no character can be silently removed.
    if "#" in raw:
        raise URLRuleError(f"URL contains a fragment ('#'): {raw!r}")
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise URLRuleError(f"unparseable URL {raw!r}: {exc}") from exc
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORT:
        raise URLRuleError(
            f"unsupported scheme {parts.scheme!r} in {raw!r} "
            f"(only http and https are accepted)")
    if parts.username is not None or parts.password is not None:
        raise URLRuleError(f"URL with username/password is rejected: {raw!r}")
    if parts.hostname is None:
        raise URLRuleError(f"URL has no host: {raw!r}")
    host = _normalize_host(parts.hostname, raw)
    try:
        port = parts.port
    except ValueError as exc:
        raise URLRuleError(f"invalid port in {raw!r}: {exc}") from exc
    if port is not None and port == _DEFAULT_PORT[scheme]:
        port = None
    path = parts.path or "/"
    query = parts.query
    _check_percent(path, "path", raw)
    if query:
        _check_percent(query, "query", raw)
    return ParsedURL(scheme=scheme, host=host, port=port, path=path,
                     query=query, original=raw)


def parse_origin(url: str) -> str:
    """Return the canonical origin string of ``url`` (validates first)."""
    return parse_url(url).origin


def canonicalize(url: str) -> str:
    """Return the display/identity form of ``url`` (validates first)."""
    return parse_url(url).identity
