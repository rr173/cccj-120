"""Localhost-only demo export site for the formfetch drill.

This is the "public data station without a copyable download link": it
serves an export page (session cookie + hidden ticket), records every
request line / header / raw body to disk, parses the submission with an
*independent* streaming multipart parser (it shares no code with the
client's encoder), and only then returns a pre-generated data file.

It binds 127.0.0.1 only and speaks plain HTTP/1.0-style requests.
"""

from __future__ import annotations

import hashlib
import http.server
import threading
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

VALID_TICKET = "TICKET-VALID"
EXPIRED_TICKET = "TICKET-EXPIRED"
SESSION_COOKIE = "sid"
SESSION_VALUE = "sid-7f3a9c"

# The pre-generated data file every successful export returns.
DATA_FILE = (b"FORMFETCH-EXPORT-DATA\n"
             + bytes((i * 37 + 11) % 256 for i in range(4096)))
DATA_SHA256 = hashlib.sha256(DATA_FILE).hexdigest()
TOO_BIG_BODY = (DATA_FILE * (2 * 1024 * 1024 // len(DATA_FILE) + 1))[
    :2 * 1024 * 1024]

_HTML_CT = ("Content-Type", "text/html; charset=utf-8")

# The export page.  Strictly closed tags (the formfetch subset), one
# form with the full menagerie of control cases, plus traps: another
# form with a same-named field, controls associated via form= before
# and after the form, an orphan control, a duplicate element id.
EXPORT_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<title>formfetch export</title>
<base href="{base}/export/" />
<base href="{base}/ignored/" />
</head>
<body>
<input type="text" id="pre-note" name="pre-note" value="before" form="export-form" />
<form id="other-form" action="elsewhere" method="post">
<input type="text" id="other-region" name="region" value="OTHER" />
<input type="submit" id="other-btn" name="go" value="other" />
</form>
<form id="export-form" action="delivery" method="post" enctype="multipart/form-data">
<input type="hidden" id="ticket" name="ticket" value="{ticket}" />
<input type="text" id="region" name="region" value="default-region" />
<input type="text" id="ro" name="ro" value="a &amp; b &lt;x&gt;" readonly="readonly" />
<input type="text" id="empty" name="empty" value="" />
<input type="text" value="nope" />
<input type="text" name="" value="nope2" />
<input type="text" id="disabled-field" name="disabled-field" value="x" disabled="disabled" />
<input type="password" id="pw" name="pw" value="" />
<input type="checkbox" id="agree" name="agree" value="yes" />
<input type="checkbox" id="news" name="news" checked="checked" />
<input type="checkbox" id="off" name="off" />
<input type="radio" id="sp-slow" name="speed" value="slow" />
<input type="radio" id="sp-fast" name="speed" value="fast" checked="checked" />
<input type="radio" id="sp-med" name="speed" value="med" checked="checked" />
<select id="format" name="format">
<option id="fmt-none" value="none" disabled="disabled">none</option>
<option id="fmt-csv" value="csv">CSV</option>
<option id="fmt-json" value="json" selected="selected">JSON</option>
<option id="fmt-xml" value="xml" selected="selected">XML</option>
</select>
<select id="priority" name="priority">
<option id="pri-low" value="low" disabled="disabled">low</option>
<option id="pri-mid">mid-text</option>
<option id="pri-high" value="high">high</option>
</select>
<select id="kinds" name="kinds" multiple="multiple">
<option id="k-temp" value="temp" selected="selected">temp</option>
<optgroup id="og-old" label="old" disabled="disabled">
<option id="k-old1" value="old1" selected="selected">old1</option>
</optgroup>
<option id="k-rain" value="rain">rain</option>
<option id="k-rain2" value="rain">rain2</option>
<option id="k-wind" value="wind" selected="selected">wind</option>
</select>
<textarea id="notes" name="notes">line1
line2 &amp; more</textarea>
<textarea id="blank" name="blank"></textarea>
<fieldset disabled="disabled">
<legend>meta <input type="text" id="legend-note" name="legend-note" value="in-legend" /></legend>
<input type="text" id="fs-off" name="fs-off" value="nope" />
<fieldset>
<input type="text" id="fs-inner" name="fs-inner" value="nope2" />
</fieldset>
</fieldset>
<fieldset disabled="disabled">
<legend>first</legend>
<legend>second <input type="text" id="fs-leg2" name="fs-leg2" value="nope3" /></legend>
</fieldset>
<fieldset disabled="disabled">
<legend>outer <fieldset disabled="disabled"><input type="text" id="fs-leg-in" name="fs-leg-in" value="nope4" /></fieldset></legend>
</fieldset>
<input type="text" id="tag1" name="tag" value="a" />
<input type="text" id="tag2" name="tag" value="b" />
<input type="text" id="dup" name="dupx1" value="d1" />
<input type="text" id="dup" name="dupx2" value="d2" />
<input type="file" id="upload" name="upload" />
<input type="file" id="uploads" name="uploads" multiple="multiple" />
<input type="file" id="attach" name="attach" />
<input type="image" id="img-btn" name="img" />
<button type="submit" id="btn-download" name="go" value="download">Download</button>
<input type="submit" id="btn-query" name="go" value="query" formaction="query?fixed=1&amp;zap=2" formmethod="get" />
<button type="submit" id="btn-urlenc" name="go" value="urlenc" formaction="alt?src=btn" formenctype="application/x-www-form-urlencoded">Alt</button>
<button type="submit" id="btn-redir" formaction="redirect">redir</button>
<button type="submit" id="btn-big" formaction="toobig">big</button>
<button type="submit" id="btn-plain" formaction="alt" formenctype="text/plain">plain</button>
<button type="submit" id="btn-dialog" formmethod="dialog">dialog</button>
<button type="submit" id="btn-disabled" disabled="disabled" name="go" value="disabled">d</button>
<input type="reset" name="rst" value="r" />
<button type="button" name="b1" value="b">x</button>
</form>
<input type="text" id="orphan" name="orphan" value="x" form="no-such-form" />
<textarea id="post-note" name="post-note" form="export-form">after</textarea>
<select id="ext-sel" name="ext-sel" form="export-form">
<option id="ext-1" value="x1" selected="selected">x1</option>
</select>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# independent streaming multipart parser (server side)


class MultipartParseError(Exception):
    pass


class FieldPart:
    def __init__(self, name, value):
        self.name = name
        self.value = value

    def to_dict(self):
        return {"kind": "text", "name": self.name, "value": self.value}


class FilePart:
    def __init__(self, name, filename, saved_as, size, sha256):
        self.name = name
        self.filename = filename
        self.saved_as = saved_as
        self.size = size
        self.sha256 = sha256

    def to_dict(self):
        return {"kind": "file", "name": self.name,
                "filename": self.filename, "saved_as": self.saved_as,
                "size": self.size, "sha256": self.sha256}


def _split_quoted(text):
    parts, cur, in_q, esc = [], [], False, False
    for ch in text:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\" and in_q:
            cur.append(ch)
            esc = True
        elif ch == '"':
            cur.append(ch)
            in_q = not in_q
        elif ch == ";" and not in_q:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _unquote(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        body = text[1:-1]
        out, i = [], 0
        while i < len(body):
            if body[i] == "\\" and i + 1 < len(body):
                out.append(body[i + 1])
                i += 2
            else:
                out.append(body[i])
                i += 1
        return "".join(out)
    return text


def parse_content_disposition(header):
    """Parse ``form-data; name="x"; filename="y"`` -> (name, filename).

    The header was decoded from UTF-8 bytes, so ``filename`` follows the
    documented convention: raw UTF-8 inside the quoted string, with
    backslash escapes for ``\\`` and ``"``.
    """
    segs = _split_quoted(header)
    if not segs or segs[0].strip().lower() != "form-data":
        raise MultipartParseError(
            f"unexpected Content-Disposition: {header!r}")
    name = filename = None
    for seg in segs[1:]:
        key, _, value = seg.partition("=")
        key = key.strip().lower()
        if key == "name":
            name = _unquote(value)
        elif key == "filename":
            filename = _unquote(value)
    if name is None:
        raise MultipartParseError("part without a name parameter")
    return name, filename


class _BoundaryStream:
    """Byte stream over a file with pushback, for boundary scanning."""

    def __init__(self, fh, delim):
        self.fh = fh
        self.delim = delim                    # b"--" + boundary
        self.marker = b"\r\n" + self.delim
        self.pending = b""
        self.eof = False

    def _more(self):
        chunk = self.fh.read(65536)
        if not chunk:
            self.eof = True
        return chunk

    def read_line(self, limit=65536):
        buf = self.pending
        self.pending = b""
        while True:
            idx = buf.find(b"\n")
            if idx >= 0:
                line, self.pending = buf[:idx + 1], buf[idx + 1:]
                if len(line) > limit:
                    raise MultipartParseError("overlong line in multipart")
                return line
            if self.eof:
                return buf
            buf += self._more()
            if len(buf) > limit and buf.find(b"\n") < 0:
                raise MultipartParseError("overlong line in multipart")

    def scan_to_boundary(self, sink):
        """Stream part-body bytes to ``sink`` up to CRLF+delimiter.

        Returns "part" (another part follows) or "end" (final
        delimiter).  Only a bounded tail of the body is ever in memory.
        """
        buf = self.pending
        self.pending = b""
        keep = len(self.marker) - 1
        while True:
            idx = buf.find(self.marker)
            if idx >= 0:
                sink.write(buf[:idx])
                rest = buf[idx + len(self.marker):]
                while len(rest) < 2 and not self.eof:
                    rest += self._more()
                if rest.startswith(b"--"):
                    self.pending = rest[2:]
                    return "end"
                if rest.startswith(b"\r\n"):
                    self.pending = rest[2:]
                    return "part"
                raise MultipartParseError(
                    f"bad bytes after boundary: {rest[:8]!r}")
            if len(buf) > keep:
                sink.write(buf[:-keep])
                buf = buf[-keep:]
            if self.eof:
                raise MultipartParseError("eof inside a part body")
            buf += self._more()


class _HashSink:
    def __init__(self, fh):
        self.fh = fh
        self.hasher = hashlib.sha256()
        self.size = 0

    def write(self, data):
        self.fh.write(data)
        self.hasher.update(data)
        self.size += len(data)


def parse_multipart_file(path, boundary: bytes, out_dir, prefix):
    """Parse a saved multipart body; file parts land on disk.

    Returns the ordered list of FieldPart/FilePart.
    """
    delim = b"--" + boundary
    parts = []
    with open(path, "rb") as fh:
        stream = _BoundaryStream(fh, delim)
        line = stream.read_line()
        if line in (delim + b"--\r\n", delim + b"--"):
            return []
        if line != delim + b"\r\n":
            raise MultipartParseError(f"bad multipart preamble: {line!r}")
        index = 0
        while True:
            headers = {}
            while True:
                line = stream.read_line()
                if line in (b"\r\n", b"\n", b""):
                    break
                key, _, value = line.decode("utf-8").partition(":")
                headers[key.strip().lower()] = value.strip()
            name, filename = parse_content_disposition(
                headers.get("content-disposition", ""))
            if filename is None:
                import io
                buf = io.BytesIO()
                term = stream.scan_to_boundary(buf)
                if buf.tell() > 8 * 1024 * 1024:
                    raise MultipartParseError("text field too large")
                parts.append(FieldPart(name, buf.getvalue()
                                       .decode("utf-8")))
            else:
                saved = Path(out_dir) / f"{prefix}-part{index}.bin"
                with open(saved, "wb") as out:
                    sink = _HashSink(out)
                    term = stream.scan_to_boundary(sink)
                parts.append(FilePart(name, filename, str(saved),
                                      sink.size,
                                      sink.hasher.hexdigest()))
            index += 1
            if term == "end":
                tail = stream.pending + fh.read()
                if tail not in (b"\r\n", b""):
                    raise MultipartParseError(
                        f"junk after final boundary: {tail[:16]!r}")
                break
    return parts


# ---------------------------------------------------------------------------
# the site


class ExportSite:
    """The localhost export site.  ``requests`` is the evidence log."""

    def __init__(self, out_dir):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.requests = []
        self.receipts = []
        self._counter = 0
        self._server = None
        self._thread = None
        self.port = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                site.handle(self)

            def do_POST(self):
                site.handle(self)

        self._server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # -- request recording ---------------------------------------------

    def handle(self, h):
        self._counter += 1
        rid = self._counter
        parsed = urlsplit(h.path)
        length = int(h.headers.get("Content-Length") or 0)
        body_path = self.out_dir / f"req-{rid:02d}-body.bin"
        got = 0
        with open(body_path, "wb") as fh:
            while got < length:
                chunk = h.rfile.read(min(65536, length - got))
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
        raw_path = self.out_dir / f"req-{rid:02d}-raw.bin"
        with open(raw_path, "wb") as fh:
            fh.write(h.raw_requestline)
            fh.write(h.headers.as_bytes())
            with open(body_path, "rb") as bf:
                while True:
                    chunk = bf.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
        record = {
            "id": rid,
            "method": h.command,
            "path": h.path,
            "request_line": h.raw_requestline.decode("latin-1")
            .rstrip("\r\n"),
            "headers": dict(h.headers.items()),
            "cookie": h.headers.get("Cookie"),
            "body_path": str(body_path),
            "raw_path": str(raw_path),
            "body_bytes": got,
            "declared_length": length,
            "complete": got == length,
        }
        self.requests.append(record)
        try:
            status, body, extra = self._route(h, parsed, rid, body_path,
                                              record)
        except Exception as exc:  # demo server: never hang the client
            status, body, extra = 500, f"site error: {exc}".encode(), []
        record["response_status"] = status
        h.send_response(status)
        for key, value in ([("Content-Length", str(len(body)))] + extra):
            h.send_header(key, value)
        h.end_headers()
        try:
            h.wfile.write(body)
        except (ConnectionResetError, BrokenPipeError):
            # The client may legitimately hang up mid-response (e.g. its
            # result-size cap tripped); that is not a server error.
            pass

    # -- routing ---------------------------------------------------------

    def _route(self, h, parsed, rid, body_path, record):
        path = parsed.path
        if h.command == "GET" and path == "/export":
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            ticket = (EXPIRED_TICKET if query.get("ticket") == "expired"
                      else VALID_TICKET)
            page = EXPORT_PAGE.format(base=self.url, ticket=ticket)
            return 200, page.encode("utf-8"), [
                _HTML_CT,
                ("Set-Cookie",
                 f"{SESSION_COOKIE}={SESSION_VALUE}; Path=/export; "
                 "HttpOnly"),
                ("Set-Cookie", "wide=1; Path=/"),
                ("Set-Cookie",
                 f"sec={SESSION_VALUE}; Path=/export; Secure"),
            ]
        if h.command == "GET" and path == "/other":
            return 200, b"<html><body>other</body></html>", [_HTML_CT]
        if h.command == "GET" and path == "/bad/nested":
            return 200, (b"<html><body><form id='a'><form id='b'>"
                         b"</form></form></body></html>"), [_HTML_CT]
        if h.command == "GET" and path == "/bad/unclosed":
            return 200, (b"<html><body><form id='f'><div></form></div>"
                         b"</body></html>"), [_HTML_CT]
        if h.command == "GET" and path == "/bad/dupform":
            return 200, (b"<html><body><form id='f'></form>"
                         b"<form id='f'></form></body></html>"), [_HTML_CT]
        if h.command == "POST" and path == "/export/redirect":
            return 302, b"redirect\n", [("Location", "/export/delivery")]
        if h.command == "POST" and path == "/export/toobig":
            return 200, TOO_BIG_BODY, [
                ("Content-Type", "application/octet-stream")]
        if h.command == "POST" and path == "/export/delivery":
            return self._handle_delivery(h, rid, body_path)
        if h.command == "POST" and path == "/export/alt":
            return self._handle_urlencoded(h, parsed, rid, body_path)
        if h.command == "GET" and path == "/export/query":
            return self._handle_query(h, parsed, rid)
        return 404, b"not found\n", []

    # -- helpers ---------------------------------------------------------

    def _check_session(self, h):
        cookie = h.headers.get("Cookie") or ""
        if f"{SESSION_COOKIE}={SESSION_VALUE}" not in cookie:
            return 403, b"missing session cookie\n", []
        return None

    def _check_ticket(self, pairs):
        tickets = [v for k, v in pairs if k == "ticket"]
        if tickets != [VALID_TICKET]:
            return 403, b"expired or unknown ticket\n", []
        return None

    def _save_receipt(self, rid, receipt):
        self.receipts.append(receipt)
        import json
        with open(self.out_dir / f"receipt-{rid:02d}.json", "w",
                  encoding="utf-8") as fh:
            json.dump(receipt, fh, ensure_ascii=False, indent=2)

    def _data_response(self):
        return 200, DATA_FILE, [
            ("Content-Type", "application/octet-stream"),
            ("Content-Disposition",
             'attachment; filename="export-data.bin"')]

    def _handle_delivery(self, h, rid, body_path):
        fail = self._check_session(h)
        if fail:
            return fail
        ctype = h.headers.get("Content-Type") or ""
        if not ctype.startswith("multipart/form-data"):
            return 400, b"expected multipart/form-data\n", []
        boundary = ctype.split("boundary=", 1)[1].strip().strip('"')
        parts = parse_multipart_file(body_path, boundary.encode("ascii"),
                                     self.out_dir, f"req-{rid:02d}")
        pairs = [(p.name, p.value) for p in parts
                 if isinstance(p, FieldPart)]
        self._save_receipt(rid, {
            "rid": rid, "kind": "multipart",
            "parts": [p.to_dict() for p in parts]})
        fail = self._check_ticket(pairs)
        if fail:
            return fail
        return self._data_response()

    def _handle_urlencoded(self, h, parsed, rid, body_path):
        fail = self._check_session(h)
        if fail:
            return fail
        with open(body_path, "rb") as fh:
            text = fh.read().decode("utf-8")
        pairs = parse_qsl(text, keep_blank_values=True)
        self._save_receipt(rid, {
            "rid": rid, "kind": "urlencoded",
            "url_query": parsed.query,
            "pairs": [[k, v] for k, v in pairs]})
        fail = self._check_ticket(pairs)
        if fail:
            return fail
        return self._data_response()

    def _handle_query(self, h, parsed, rid):
        fail = self._check_session(h)
        if fail:
            return fail
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        self._save_receipt(rid, {
            "rid": rid, "kind": "get",
            "raw_query": parsed.query,
            "pairs": [[k, v] for k, v in pairs]})
        fail = self._check_ticket(pairs)
        if fail:
            return fail
        return self._data_response()
