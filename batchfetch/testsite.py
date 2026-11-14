"""Controllable two-port local HTTP site for drills and integration tests.

The server is standard-library only and binds exclusively to ``127.0.0.1``;
no real website is ever contacted.  Two
:class:`http.server.ThreadingHTTPServer` instances (two ports, therefore
two distinct origins and two separate budgets) share one
:class:`ScriptedSite` state object.

Behavior is chosen purely by the request path so a test/drill only needs
manifest URLs:

``/ok[/...]``
    200 with a small deterministic body.
``/empty``
    200 with a zero-length body (defined SHA-256 of the empty string).
``/fail500``
    500 with a short body (a real HTTP response, never retried).
``/redirect?to=<absolute-or-relative-url>&status=<n>``
    redirect (default 302) with an explicit ``Location``.
``/loop-a`` / ``/loop-b``
    the two ports redirect at each other to form an endless ring.
``/truncated?len=<n>``
    declares ``Content-Length: <n>`` but closes after fewer bytes.
``/oversize?len=<n>``
    streams ``len`` bytes with no cap on the server side so the client's
    body cap can trip mid-stream.
``/gate?name=<name>``
    blocks inside the handler on a named :class:`threading.Event` until a
    control call releases it; the receipt is recorded *before* blocking,
    which is what lets a test prove the request reached the server while
    the response is still held.

Control (same server, path prefix ``/__control``):

``/__control/open?name=<name>``
    release every handler blocked on the named gate;
``/__control/snapshot``
    return the full server-side receipt log (method, path, headers,
    receive order) as JSON -- the independent evidence used to verify the
    executor's own timeline;
``/__control/reset``
    clear receipts and gates;
``/__control/shutdown``
    stop both listening sockets.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlsplit


class ScriptedSite:
    """Shared, thread-safe state of the two test origins."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.receipts: list[dict] = []
        self._gates: dict[str, threading.Event] = {}
        self._counter = 0

    def reset(self) -> None:
        with self._lock:
            self.receipts.clear()
            self._gates.clear()

    def gate(self, name: str) -> threading.Event:
        with self._lock:
            event = self._gates.get(name)
            if event is None:
                event = threading.Event()
                self._gates[name] = event
            return event

    def open_gate(self, name: str) -> int:
        event = self.gate(name)
        event.set()
        with self._lock:
            return sum(1 for r in self.receipts
                       if r["path"].startswith("/gate"))

    def record(self, *, port: int, method: str, path: str,
               headers: dict) -> int:
        with self._lock:
            self._counter += 1
            seq = self._counter
            self.receipts.append({
                "seq": seq,
                "port": port,
                "method": method,
                "path": path,
                "headers": headers,
            })
        return seq

    def snapshot(self) -> dict:
        with self._lock:
            return {"receipts": [dict(r) for r in self.receipts]}


class _Handler(BaseHTTPRequestHandler):
    site: ScriptedSite  # injected per server subclass
    peer_port: int      # the *other* origin's port, for the loop script

    # Quieter test output; the drill can re-enable by subclassing.
    def log_message(self, fmt, *args) -> None:  # noqa: A003
        return

    # -- helpers --------------------------------------------------------
    def _send(self, status: int, body: bytes = b"", *,
              content_type: str = "text/plain; charset=utf-8",
              extra_headers: Optional[dict] = None,
              declared_length: Optional[int] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length",
                         str(declared_length
                             if declared_length is not None else len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _qs(self) -> dict:
        query = parse_qs(urlsplit(self.path).query)
        return {k: v[0] for k, v in query.items()}

    # -- routing --------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        path, query = parts.path, parts.query
        headers = {k: v for k, v in self.headers.items()}
        seq = self.site.record(port=self.server.server_address[1],
                               method="GET", path=self.path,
                               headers=headers)
        try:
            if path == "/__control/snapshot":
                self._send(200, json.dumps(
                    self.site.snapshot(), ensure_ascii=False).encode(),
                    content_type="application/json")
                return
            if path == "/__control/reset":
                self.site.reset()
                self._send(200, b'{"reset": true}',
                    content_type="application/json")
                return
            if path == "/__control/open":
                name = self._qs().get("name", "default")
                released = self.site.open_gate(name)
                self._send(200, json.dumps(
                    {"opened": name, "waiters_released": released}).encode(),
                    content_type="application/json")
                return
            if path == "/__control/shutdown":
                self._send(200, b'{"shutting_down": true}',
                    content_type="application/json")
                threading.Thread(target=self.server.shutdown,
                                 daemon=True).start()
                return
            if path.startswith("/gate"):
                name = self._qs().get("name", "default")
                # The receipt is already recorded above, *before* blocking:
                # the client sees the request arrive while the response is
                # still held behind the gate.
                if not self.site.gate(name).wait(timeout=30):
                    self._send(504, b'{"error":"gate timeout"}')
                    return
                self._send(200,
                           ('{"gate": ' + json.dumps(name) + ', "seq": '
                            + str(seq) + "}").encode())
                return
            if path == "/empty":
                self._send(200, b"")
                return
            if path == "/fail500":
                self._send(500, b"simulated server failure")
                return
            if path == "/truncated":
                declared = int(self._qs().get("len", "64"))
                payload = b"x" * max(1, declared // 2)
                # Lie about the length and close early.
                self._send(200, payload, declared_length=declared)
                self.close_connection = True
                return
            if path == "/oversize":
                length = int(self._qs().get("len", str(2 * 1024 * 1024)))
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(length))
                self.end_headers()
                chunk = b"a" * 65536
                remaining = length
                while remaining > 0:
                    take = min(remaining, len(chunk))
                    self.wfile.write(chunk[:take])
                    remaining -= take
                return
            if path == "/redirect":
                params = self._qs()
                target = params.get("to", "/ok")
                status = int(params.get("status", "302"))
                self._send(status, b"", extra_headers={"Location": target})
                return
            if path == "/loop-a":
                target = f"http://127.0.0.1:{self.peer_port}/loop-b"
                self._send(302, b"", extra_headers={"Location": target})
                return
            if path == "/loop-b":
                target = f"http://127.0.0.1:{self.peer_port}/loop-a"
                self._send(302, b"", extra_headers={"Location": target})
                return
            if path == "/ok" or path.startswith("/ok/"):
                self._send(200,
                           ('{"ok": true, "seq": ' + str(seq) + ', "path": '
                            + json.dumps(path) + "}").encode())
                return
            self._send(404, b'{"error": "unknown script path"}',
                       content_type="application/json")
        except (BrokenPipeError, ConnectionResetError):
            # The client may close early (body cap, cancellation); that is
            # part of the scripted behavior, not a server crash.
            pass

    do_HEAD = do_GET


class DualPortSite:
    """Two localhost HTTP servers backed by one :class:`ScriptedSite`."""

    def __init__(self, *, host: str = "127.0.0.1",
                 port_a: int = 0, port_b: int = 0) -> None:
        self.site = ScriptedSite()
        self.host = host

        handler_a = type("_HandlerA", (_Handler,),
                         {"site": self.site, "peer_port": None})
        handler_b = type("_HandlerB", (_Handler,),
                         {"site": self.site, "peer_port": None})
        self.server_a = ThreadingHTTPServer((host, port_a), handler_a)
        self.server_b = ThreadingHTTPServer((host, port_b), handler_b)
        self.port_a = self.server_a.server_address[1]
        self.port_b = self.server_b.server_address[1]
        # Backfill the cross-port redirect targets.
        handler_a.peer_port = self.port_b
        handler_b.peer_port = self.port_a
        self._threads: list[threading.Thread] = []

    @property
    def origin_a(self) -> str:
        return f"http://{self.host}:{self.port_a}"

    @property
    def origin_b(self) -> str:
        return f"http://{self.host}:{self.port_b}"

    def start(self) -> "DualPortSite":
        for server, name in ((self.server_a, "site-a"),
                             (self.server_b, "site-b")):
            thread = threading.Thread(
                target=server.serve_forever, name=f"test-http-{name}",
                daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def stop(self) -> None:
        for server in (self.server_a, self.server_b):
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        for thread in self._threads:
            thread.join(timeout=5)

    def __enter__(self) -> "DualPortSite":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- control convenience -------------------------------------------
    def control_url(self, which: str, action: str, **params) -> str:
        port = self.port_a if which == "a" else self.port_b
        query = "&".join(f"{k}={v}" for k, v in params.items())
        suffix = f"/__control/{action}" + (f"?{query}" if query else "")
        return f"http://{self.host}:{port}{suffix}"

    def receipts(self) -> list[dict]:
        return self.site.snapshot()["receipts"]
