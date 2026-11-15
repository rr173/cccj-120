"""Byte-level request-body construction.

Conventions (also documented in the module README):

* ``application/x-www-form-urlencoded``: UTF-8, space encoded as ``+``,
  duplicate keys kept in order; newlines in every string value are
  normalized to CRLF *before* percent-encoding;
* ``multipart/form-data``: one part per entry, in entry order; file
  controls expand to one part per selected file at the control's own
  position; text parts carry no Content-Type, file parts always carry
  ``Content-Type: application/octet-stream``;
* part ``name`` and ``filename`` are placed inside a quoted-string as
  raw UTF-8 bytes with ``\\`` and ``"`` backslash-escaped (no RFC 5987
  ``filename*``); names/filenames containing CR or LF are rejected;
* ``filename`` is the local file's basename; an unselected file control
  yields a part with an empty filename and an empty body (distinct from
  a selected zero-byte file);
* the body is produced as a stream: file content is read in chunks at
  send time, never fully resident in memory, and the announced
  Content-Length is computed from the same segment list the sender
  replays.
"""

from __future__ import annotations

import os
import re
from urllib.parse import quote_plus

from .errors import EncodeError
from .forms import FileEntry

CRLF = b"\r\n"
_CHUNK = 64 * 1024
_BOUNDARY_RE = re.compile(r"^[A-Za-z0-9'()+_,./:=?-]{1,70}$")


def normalize_newlines(value: str) -> str:
    """CRLF/CR/LF -> CRLF, applied to every submitted string value."""
    return (value.replace("\r\n", "\n")
                 .replace("\r", "\n")
                 .replace("\n", "\r\n"))


def urlencode_entries(entries) -> bytes:
    """Ordered, duplicate-preserving urlencoding of the entries."""
    parts = []
    for entry in entries:
        if isinstance(entry, FileEntry):
            if entry.path is not None:
                raise EncodeError(
                    "file uploads require POST multipart/form-data")
            value = ""
        else:
            value = normalize_newlines(entry.value)
        try:
            parts.append(
                quote_plus(entry.name, encoding="utf-8", errors="strict")
                + "="
                + quote_plus(value, encoding="utf-8", errors="strict"))
        except UnicodeEncodeError as exc:
            raise EncodeError(
                f"control name/value is not encodable as UTF-8: {exc}") \
                from exc
    # "&".join of ascii-safe quoted parts is ascii
    return "&".join(parts).encode("ascii")


def _escape_quoted(text: str, what: str) -> str:
    if "\r" in text or "\n" in text:
        raise EncodeError(
            f"{what} must not contain CR/LF characters: {text!r}")
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _utf8(text: str, what: str) -> bytes:
    try:
        return text.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise EncodeError(f"{what} is not encodable as UTF-8: {exc}") \
            from exc


class MultipartBody:
    """A multipart body as a replayable segment list.

    ``content_length`` is exact: it is the sum of the same segments that
    :meth:`iter_chunks` replays, so the announced length always matches
    the bytes on the wire.
    """

    def __init__(self, segments, boundary):
        self._segments = segments
        self.boundary = boundary
        self.content_type = f"multipart/form-data; boundary={boundary}"
        self.content_length = sum(
            len(seg[1]) if seg[0] == "bytes" else seg[2]
            for seg in segments)

    def iter_chunks(self, chunk_size=_CHUNK):
        for seg in self._segments:
            if seg[0] == "bytes":
                yield seg[1]
            else:
                _, path, size = seg
                with open(path, "rb") as fh:
                    actual = os.fstat(fh.fileno()).st_size
                    if actual != size:
                        raise EncodeError(
                            f"file changed size ({size} -> {actual}) "
                            f"between preparation and send: {path}")
                    remaining = size
                    while remaining > 0:
                        chunk = fh.read(min(chunk_size, remaining))
                        if not chunk:
                            raise EncodeError(
                                f"file ended early while sending: {path}")
                        remaining -= len(chunk)
                        yield chunk


def build_multipart(entries, boundary: str) -> MultipartBody:
    if not _BOUNDARY_RE.match(boundary):
        raise EncodeError(
            f"invalid multipart boundary {boundary!r}: 1-70 chars of "
            "alphanumerics and '()+_,-./:=? only")
    delim = boundary.encode("ascii")
    segments = []

    def push(data: bytes):
        segments.append(("bytes", data))

    for entry in entries:
        name = _utf8(_escape_quoted(entry.name, "control name"),
                     "control name")
        if isinstance(entry, FileEntry):
            if entry.path is None:
                filename = b""
                path = None
                size = 0
            else:
                path = entry.path
                filename = _utf8(
                    _escape_quoted(os.path.basename(path), "filename"),
                    "filename")
                try:
                    size = os.stat(path).st_size
                except OSError as exc:
                    raise EncodeError(
                        f"cannot stat upload file {path}: {exc}") from exc
            push(b"--" + delim + CRLF
                 + b'Content-Disposition: form-data; name="' + name
                 + b'"; filename="' + filename + b'"' + CRLF
                 + b"Content-Type: application/octet-stream" + CRLF
                 + CRLF)
            if path is not None:
                segments.append(("file", path, size))
            push(CRLF)
        else:
            value = _utf8(normalize_newlines(entry.value),
                          "control value")
            push(b"--" + delim + CRLF
                 + b'Content-Disposition: form-data; name="' + name + b'"'
                 + CRLF + CRLF)
            push(value)
            push(CRLF)
    push(b"--" + delim + b"--" + CRLF)
    return MultipartBody(segments, boundary)
