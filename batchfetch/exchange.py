"""One HTTP exchange over the standard library.

An *exchange* is a single request/response pair (redirects are separate
exchanges driven by the scheduler, never library auto-follows).  The
exchange occupies its global and origin slots from immediately before the
request headers go out until the response body has been fully read, the
body cap trips, the stream truncates, or the exchange fails; the caller
owns the slot bookkeeping and closes the connection in ``finally``.

The body is streamed in fixed chunks: only one chunk plus the SHA-256
hasher state and the byte counter live in memory -- the full body is never
retained.  An empty body still yields a defined digest (the SHA-256 of
the empty byte string).  Two conditions make an otherwise received
response incomplete:

* more than ``max_body_bytes`` bytes arrive (the connection is closed as
  soon as the cap is crossed);
* a ``Content-Length`` was declared but the source ends early
  (:class:`http.client.IncompleteRead`).

4xx/5xx responses are normal responses; connection failures and read
timeouts are exchange failures the batch records as ``failed`` jobs.
"""

from __future__ import annotations

import hashlib
import http.client
import socket
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

_READ_CHUNK = 64 * 1024
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class ExchangeError(Exception):
    """A transport-level failure (never a 4xx/5xx response)."""

    def __init__(self, kind: str, reason: str):
        super().__init__(f"{kind}: {reason}")
        self.kind = kind
        self.reason = reason


@dataclass
class ExchangeResult:
    """Everything one exchange produced (body content deliberately absent)."""

    ok: bool
    url: str                       # address this exchange actually requested
    origin: str
    status: Optional[int] = None
    reason: Optional[str] = None
    location: Optional[str] = None  # raw Location header, if any
    body_bytes: int = 0
    body_sha256: Optional[str] = None
    headers: dict = field(default_factory=dict)
    failure_kind: Optional[str] = None
    failure_reason: Optional[str] = None

    @property
    def is_redirect(self) -> bool:
        return self.ok and self.status in REDIRECT_STATUSES

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "url": self.url,
            "origin": self.origin,
            "status": self.status,
            "reason": self.reason,
            "location": self.location,
            "body_bytes": self.body_bytes,
            "body_sha256": self.body_sha256,
            "failure_kind": self.failure_kind,
            "failure_reason": self.failure_reason,
        }


def _connect(parts, timeout: float):
    host = parts.hostname
    port = parts.port
    if parts.scheme.lower() == "https":
        conn = http.client.HTTPSConnection(
            host, port if port is not None else 443, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(
            host, port if port is not None else 80, timeout=timeout)
    return conn


def _target(url: str):
    parts = urlsplit(url)
    return parts, parts.hostname or ""


def perform_exchange(url: str, origin: str, *, timeout: float,
                     max_body_bytes: int) -> ExchangeResult:
    """Perform exactly one HTTP request and stream the response body.

    Raises nothing for transport problems: they come back as
    ``ok=False`` results with a stable ``failure_kind``
    (``connection_error`` / ``read_timeout`` / ``protocol_error`` /
    ``body_too_large`` is instead attached to an incomplete response).
    """
    parts, _host = _target(url)
    target_path = parts.path or "/"
    if parts.query:
        target_path += "?" + parts.query
    deadline = time.monotonic() + timeout
    conn = None
    try:
        conn = _connect(parts, timeout)
        conn.connect()
        conn.putrequest("GET", target_path, skip_host=False,
                        skip_accept_encoding=True)
        conn.putheader("Accept-Encoding", "identity")
        conn.putheader("Connection", "close")
        conn.endheaders()
        response = conn.getresponse()

        def _remaining() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"exchange exceeded request timeout of {timeout}s")
            # Keep each individual socket operation bounded by the total
            # exchange deadline as well.
            sock = getattr(conn, "sock", None)
            if sock is not None:
                sock.settimeout(remaining)
            return remaining

        status = response.status
        location = response.getheader("Location")
        headers = {k.lower(): v for k, v in response.getheaders()}
        declared_length = response.length  # None when not declared

        hasher = hashlib.sha256()
        total = 0
        truncated = False
        over_cap = False
        try:
            while True:
                _remaining()
                try:
                    chunk = response.read(_READ_CHUNK)
                except http.client.IncompleteRead as exc:
                    # Declared Content-Length not fully delivered: keep the
                    # bytes that did arrive, then mark the body incomplete.
                    partial = exc.partial
                    if partial:
                        hasher.update(partial)
                        total += len(partial)
                    truncated = True
                    break
                if not chunk:
                    break
                total += len(chunk)
                hasher.update(chunk)
                if total > max_body_bytes:
                    over_cap = True
                    break
        except (socket.timeout, TimeoutError) as exc:
            return _failure(url, origin, "read_timeout", str(exc))
        except (http.client.HTTPException, OSError) as exc:
            return _failure(url, origin, "protocol_error", str(exc))

        if truncated or (declared_length is not None
                         and total < declared_length and not over_cap):
            return ExchangeResult(
                ok=False, url=url, origin=origin, status=status,
                reason=response.reason, body_bytes=total,
                body_sha256=hasher.hexdigest(), headers=headers,
                failure_kind="truncated_body",
                failure_reason="server closed the connection before the "
                               f"declared Content-Length ({declared_length}) "
                               f"was delivered (got {total})")
        if over_cap:
            return ExchangeResult(
                ok=False, url=url, origin=origin, status=status,
                reason=response.reason, body_bytes=total,
                body_sha256=hasher.hexdigest(), headers=headers,
                failure_kind="body_too_large",
                failure_reason=f"body exceeded max_body_bytes={max_body_bytes}")
        return ExchangeResult(
            ok=True, url=url, origin=origin, status=status,
            reason=response.reason, location=location,
            body_bytes=total,
            body_sha256=hasher.hexdigest() if total else _EMPTY_SHA256,
            headers=headers)
    except (socket.timeout, TimeoutError) as exc:
        return _failure(url, origin, "read_timeout", str(exc))
    except (ConnectionError, OSError, http.client.HTTPException) as exc:
        kind = "connection_error"
        if isinstance(exc, http.client.HTTPException):
            kind = "protocol_error"
        return _failure(url, origin, kind, str(exc))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # pragma: no cover - close is best effort
                pass


def _failure(url: str, origin: str, kind: str, reason: str) -> ExchangeResult:
    return ExchangeResult(ok=False, url=url, origin=origin,
                          failure_kind=kind, failure_reason=reason)
