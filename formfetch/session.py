"""Cookie-aware HTTP session (stdlib urllib, redirects disabled).

One :class:`FormSession` holds an in-memory cookie jar.  Cookie
domain/path/Secure handling is delegated to
:class:`http.cookiejar.CookieJar` -- cookies are attached per request by
the jar, never concatenated by hand, so a cookie only travels to the
addresses its own scope allows.

Both the page GET and the submit go through the same opener.  Redirects
are never followed automatically: any non-2xx status (including 3xx)
raises :class:`ResponseStatusError`.  The response body of a failed
request is read only as a small diagnostic snippet.
"""

from __future__ import annotations

import hashlib
import http.client
import http.cookiejar
import urllib.error
import urllib.request
from dataclasses import dataclass

from .errors import NetworkError, ResponseStatusError, SizeLimitError

_READ_CHUNK = 64 * 1024
_ERROR_SNIPPET_BYTES = 16 * 1024
USER_AGENT = "formfetch/1.0 (python stdlib)"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then surfaces the 3xx as an HTTPError


@dataclass
class FetchedPage:
    url: str
    status: int
    headers: object
    body: bytes


@dataclass
class SentResult:
    status: int
    reason: str
    headers: object
    bytes_written: int
    sha256: str


class FormSession:
    def __init__(self, *, timeout=30.0, user_agent=USER_AGENT):
        self.timeout = timeout
        self.user_agent = user_agent
        self.cookie_jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar),
            _NoRedirect())

    # -- internals ------------------------------------------------------

    def _open(self, url, *, method, data=None, extra_headers=None):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", self.user_agent)
        req.add_header("Accept-Encoding", "identity")
        for key, value in (extra_headers or {}).items():
            req.add_header(key, value)
        try:
            return self._opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            snippet = b""
            try:
                snippet = exc.read(_ERROR_SNIPPET_BYTES)
            except Exception:
                pass
            raise ResponseStatusError(
                f"{method} {url} failed: HTTP {exc.code} {exc.reason}",
                status=exc.code, url=url,
                location=exc.headers.get("Location") if exc.headers
                else None,
                snippet=snippet.decode("utf-8", "replace")[:300]) from exc
        except (urllib.error.URLError, http.client.HTTPException, OSError,
                TimeoutError) as exc:
            raise NetworkError(f"{method} {url} failed: {exc}") from exc

    # -- public -----------------------------------------------------------

    def get_bytes(self, url, *, max_bytes) -> FetchedPage:
        """GET ``url`` (2xx only) and return the body, capped at
        ``max_bytes``."""
        resp = self._open(url, method="GET")
        try:
            body = resp.read(max_bytes + 1)
        except (http.client.HTTPException, OSError, TimeoutError) as exc:
            raise NetworkError(
                f"GET {url}: error while reading the body: {exc}") from exc
        finally:
            resp.close()
        if len(body) > max_bytes:
            raise SizeLimitError(
                f"GET {url}: page body exceeds the limit of {max_bytes} "
                "bytes")
        return FetchedPage(url=url, status=resp.status,
                           headers=resp.headers, body=body)

    def send_to_file(self, *, method, url, content_type, body,
                     content_length, sink, max_bytes) -> SentResult:
        """Send one request and stream the (2xx) response body into
        ``sink`` (a binary file object), capped at ``max_bytes``."""
        headers = {}
        if content_type is not None:
            headers["Content-Type"] = content_type
        if content_length is not None:
            headers["Content-Length"] = str(content_length)
        resp = self._open(url, method=method, data=body,
                          extra_headers=headers)
        hasher = hashlib.sha256()
        total = 0
        try:
            while True:
                chunk = resp.read(_READ_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise SizeLimitError(
                        f"{method} {url}: response body exceeds the limit "
                        f"of {max_bytes} bytes")
                hasher.update(chunk)
                sink.write(chunk)
        except (http.client.HTTPException, OSError, TimeoutError) as exc:
            raise NetworkError(
                f"{method} {url}: error while reading the response body: "
                f"{exc}") from exc
        finally:
            resp.close()
        return SentResult(status=resp.status, reason=resp.reason,
                          headers=resp.headers, bytes_written=total,
                          sha256=hasher.hexdigest())
