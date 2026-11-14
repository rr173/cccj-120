"""Origin identity and match-target normalization.

Origin identity (the cache key):
    lowercase scheme + normalized host + port, where the host is
    lowercased, Unicode names are IDNA-encoded to ASCII, IPv6 literals
    are canonically compressed, and the default port (http 80, https 443)
    is omitted.  http and https never share a cache entry, and neither
    do two different non-default ports.

Match target (used *only* for rule matching; the caller's URL is never
rewritten into a different fetch address):
    path (+ "?" + query when the query is non-empty); an empty path
    becomes "/" and the fragment never participates.  The target is then
    percent-normalized to bytes:

    * non-ASCII characters are UTF-8 encoded, each byte -> uppercase %HH;
    * an existing %HH naming an unreserved ASCII byte (A-Z a-z 0-9 -._~)
      is decoded (so %7E and ~ are the same);
    * any other %HH is kept as three ASCII bytes with uppercase hex, so
      %2F never becomes a real "/" separator and %3F never becomes a
      real "?" query separator;
    * malformed or non-hex % escapes are rejected.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urlsplit

from .errors import InvalidURLError

_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_HEXDIGITS = frozenset("0123456789abcdefABCDEF")
_DEFAULT_PORT = {"http": 80, "https": 443}


@dataclass(frozen=True)
class Origin:
    """Normalized origin: the unit of cache isolation."""

    scheme: str
    host: str
    port: Optional[int]  # None means the scheme default port

    @property
    def key(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        if self.port is None:
            return f"{self.scheme}://{host}"
        return f"{self.scheme}://{host}:{self.port}"

    def __str__(self) -> str:
        return self.key


def normalize_match_bytes(text: str) -> bytes:
    """Percent-normalize a path+query string or a rule value to bytes.

    Raises :class:`InvalidURLError` on malformed or non-hex percent
    escapes.  Kept percent-encodings participate in matching and length
    computation as three ASCII bytes.
    """
    out = bytearray()
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "%":
            if i + 2 < n and text[i + 1] in _HEXDIGITS and text[i + 2] in _HEXDIGITS:
                value = int(text[i + 1:i + 3], 16)
                if chr(value) in _UNRESERVED:
                    out.append(value)
                else:
                    out += b"%"
                    out += format(value, "02X").encode("ascii")
                i += 3
            else:
                raise InvalidURLError(
                    f"malformed percent escape at offset {i} in {text!r}")
        elif ord(ch) < 0x80:
            out.append(ord(ch))
            i += 1
        else:
            for byte in ch.encode("utf-8"):
                out += b"%"
                out += format(byte, "02X").encode("ascii")
            i += 1
    return bytes(out)


def _normalize_host(hostname: str) -> str:
    host = hostname.lower()
    if not host:
        raise InvalidURLError("URL has an empty host")
    if ":" in host:  # IPv6 literal
        try:
            return ipaddress.IPv6Address(host).compressed
        except ValueError as exc:
            raise InvalidURLError(f"invalid IPv6 host {hostname!r}") from exc
    try:
        return host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError) as exc:
        raise InvalidURLError(f"invalid host name {hostname!r}") from exc


def _origin_from_parts(parts) -> Origin:
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise InvalidURLError(
            f"unsupported scheme {parts.scheme!r} (only http/https are admitted)")
    if parts.username is not None or parts.password is not None:
        raise InvalidURLError("URLs with username/password are rejected")
    if parts.hostname is None:
        raise InvalidURLError("URL has no host")
    host = _normalize_host(parts.hostname)
    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidURLError(f"invalid port: {exc}") from exc
    if port is not None and port == _DEFAULT_PORT[scheme]:
        port = None
    return Origin(scheme, host, port)


def parse_origin(url: str) -> Origin:
    """Normalize a full URL (or a bare origin string) to its Origin."""
    if not isinstance(url, str):
        raise TypeError(f"url must be str, got {type(url).__name__}")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise InvalidURLError(f"unparseable URL {url!r}: {exc}") from exc
    return _origin_from_parts(parts)


def normalize_url(url: str) -> Tuple[Origin, bytes]:
    """Validate a candidate URL; return (origin, match-target bytes).

    The match target is path (+ "?" + query when the query is non-empty);
    an empty path becomes "/" and the fragment never participates.
    """
    if not isinstance(url, str):
        raise TypeError(f"url must be str, got {type(url).__name__}")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise InvalidURLError(f"unparseable URL {url!r}: {exc}") from exc
    origin = _origin_from_parts(parts)
    path = parts.path or "/"
    target_text = path + ("?" + parts.query if parts.query else "")
    try:
        target = normalize_match_bytes(target_text)
    except InvalidURLError as exc:
        raise InvalidURLError(f"URL {url!r} rejected: {exc}") from exc
    return origin, target
