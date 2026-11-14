"""Streaming WARC/1.1 parser with strict, Content-Length-driven boundaries.

Input contract (see WARCDB.md for the full spec):

  * uncompressed WARC/1.1 byte stream, any number of records in sequence;
  * record = version line, header section, blank line, content block of
    exactly Content-Length bytes, then the record separator CRLF CRLF;
  * header field names are case-insensitive; original values, order and
    duplicates are preserved for non-identity fields;
  * identity/boundary fields (WARC-Record-ID, WARC-Type, WARC-Date,
    Content-Length, WARC-Target-URI, WARC-Refers-To, WARC-Profile,
    WARC-Block-Digest, WARC-Payload-Digest) must appear at most once --
    duplicates are rejected;
  * WARC-Date must carry a timezone; instants are compared in UTC;
  * WARC-Target-URI is used verbatim (raw string) as the business key;
  * the parser never resynchronises by scanning for the next "WARC/1.1"
    string: a malformed record fails the whole parse with a precise error.

Only record types 'response' and 'revisit' (profile
identical-payload-digest) are supported.  The content block of a response
record is one complete HTTP/1.0 or HTTP/1.1 response with Content-Length,
no Transfer-Encoding and no Content-Encoding; the content block of a
revisit record is exactly one HTTP response header section (no body).

The parser reads from any object with read(n) -> bytes and tell() -> int,
in chunks of at most `limits.read_chunk`, holding only the current header
sections (bounded by `limits.max_header_bytes`) plus one read chunk in
memory.  Payload bytes are streamed through hashers, never buffered.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .errors import (
    DigestMismatchError,
    HttpFormatError,
    UnsupportedDigestError,
    UnsupportedRecordTypeError,
    WarcFormatError,
    WarcLimitError,
    WarcTruncatedError,
)

VERSION_LINE = b"WARC/1.1\r\n"
RECORD_SEPARATOR = b"\r\n\r\n"
HEADER_END = b"\r\n\r\n"

# WARC-Profile URIs accepted for revisit records (1.0 and 1.1 spellings).
REVISIT_PROFILES = frozenset({
    "http://netpreserve.org/warc/1.0/revisit/identical-payload-digest",
    "http://netpreserve.org/warc/1.1/revisit/identical-payload-digest",
})

# Fields that determine record identity or byte boundaries: at most one
# occurrence each, duplicates are rejected outright.
_SINGLETON_FIELDS = frozenset({
    "warc-record-id", "warc-type", "warc-date", "content-length",
    "warc-target-uri", "warc-refers-to", "warc-profile",
    "warc-block-digest", "warc-payload-digest",
})

_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_STATUS_LINE_RE = re.compile(r"^HTTP/(\d+)\.(\d+) (\d{3})(?: (.*))?$")
_DIGITS_RE = re.compile(r"^[0-9]+$")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Limits:
    """Caps applied while parsing one archive.

    read_chunk:       granularity of stream reads (parse block size)
    max_header_bytes: cap on the WARC header section and on the HTTP
                      header section, each including its CRLF CRLF
    max_block_bytes:  cap on one WARC content block
    """
    read_chunk: int = 65536
    max_header_bytes: int = 65536
    max_block_bytes: int = 1 << 30

    def __post_init__(self):
        if not isinstance(self.read_chunk, int) or self.read_chunk < 1:
            raise ValueError("read_chunk must be a positive int")
        if not isinstance(self.max_header_bytes, int) or self.max_header_bytes < 64:
            raise ValueError("max_header_bytes must be an int >= 64")
        if not isinstance(self.max_block_bytes, int) or self.max_block_bytes < 1:
            raise ValueError("max_block_bytes must be a positive int")


@dataclass(frozen=True)
class HeaderField:
    """One header field: original name case, OWS-trimmed value."""
    name: str
    value: str

    @property
    def lower(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class HttpHead:
    """Parsed HTTP response header section."""
    version: str            # "1.0" or "1.1"
    status: int
    reason: str
    headers: tuple[HeaderField, ...]
    head_len: int           # bytes of the whole header section incl. CRLFCRLF
    content_length: int     # declared HTTP Content-Length


@dataclass(frozen=True)
class ParsedRecord:
    """Everything the fact store needs about one WARC record.

    Offsets are absolute byte positions in the original (plaintext) WARC
    stream.  No body bytes are kept, only digests and ranges.
    """
    record_id: str
    record_type: str                 # 'response' | 'revisit'
    target_uri: str
    date_utc: str                    # normalised ISO-8601, UTC, microseconds
    date_us: int                     # the same instant in epoch microseconds
    refers_to: str | None
    block_digest: str                # declared WARC-Block-Digest (sha256 hex)
    payload_digest: str              # declared WARC-Payload-Digest (sha256 hex)
    http: HttpHead
    record_offset: int
    record_length: int               # whole record incl. trailing CRLF CRLF
    record_sha256: str               # sha256 over the whole record bytes
    content_offset: int              # start of the WARC content block
    content_length: int
    body_offset: int | None          # response only: payload range ...
    body_length: int | None          # ... inside the original WARC stream
    warc_headers: tuple[HeaderField, ...] = field(default=())


# --------------------------------------------------------------------------
# scalar field parsing helpers
# --------------------------------------------------------------------------

def parse_instant(value: str, *, what: str = "WARC-Date") -> tuple[str, int]:
    """Parse an ISO-8601 timestamp that MUST carry a timezone.

    Returns (normalised UTC ISO string, epoch microseconds).  Naive
    timestamps are rejected: instants are always compared in UTC.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise WarcFormatError(f"{what} {value!r} is not a valid ISO-8601 timestamp")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise WarcFormatError(f"{what} {value!r} has no timezone offset")
    dt = dt.astimezone(timezone.utc)
    delta = dt - _EPOCH
    us = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    return dt.isoformat(timespec="microseconds"), us


def parse_digest_field(value: str, *, field_name: str) -> str:
    """'sha256:<64 hex>' is the only accepted digest form."""
    algo, sep, hexd = value.strip().partition(":")
    if not sep:
        raise WarcFormatError(f"{field_name} {value!r} is not '<algorithm>:<hex>'")
    if algo.strip().lower() != "sha256":
        raise UnsupportedDigestError(
            f"{field_name} uses algorithm {algo!r}; only sha256 is supported"
        )
    hexd = hexd.strip().lower()
    if len(hexd) != 64 or any(c not in "0123456789abcdef" for c in hexd):
        raise WarcFormatError(f"{field_name} {value!r} is not 64 hex digits")
    return hexd


def parse_length_field(value: str, *, field_name: str = "Content-Length") -> int:
    """A bare non-negative decimal integer; anything else is rejected."""
    v = value.strip()
    if not _DIGITS_RE.match(v):
        raise WarcFormatError(f"illegal {field_name} value {value!r}")
    return int(v)


def _decode_text(raw: bytes, *, what: str) -> str:
    try:
        return raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise WarcFormatError(f"{what} is not valid UTF-8") from None


def _parse_header_lines(blob: bytes, *, offset: int, error_cls, what: str
                        ) -> tuple[HeaderField, ...]:
    """Parse 'Name: value' lines (already split off the status/version line).

    Field names are case-insensitive tokens; values keep their original
    bytes minus surrounding whitespace.  Order and duplicates are
    preserved.  Folded (continuation) lines are not supported.
    """
    if not blob:
        return ()
    fields = []
    for line in blob.split(b"\r\n"):
        if not line:
            continue
        if line[:1] in (b" ", b"\t"):
            raise error_cls(
                f"{what} at offset {offset}: folded/continuation header "
                f"lines are not supported"
            )
        name_raw, sep, value_raw = line.partition(b":")
        if not sep:
            raise error_cls(f"{what} at offset {offset}: header line without ':'")
        name = _decode_text(name_raw, what=f"{what} field name")
        if not _TOKEN_RE.match(name):
            raise error_cls(
                f"{what} at offset {offset}: invalid field name {name!r}"
            )
        value = _decode_text(value_raw, what=f"{what} value of {name!r}")
        fields.append(HeaderField(name, value.strip(" \t")))
    return tuple(fields)


def _singleton(fields: tuple[HeaderField, ...], lower_name: str, *,
               required: bool, error_cls, what: str) -> str | None:
    vals = [f.value for f in fields if f.lower == lower_name]
    if len(vals) > 1:
        raise error_cls(f"{what}: duplicate {lower_name} fields are rejected")
    if not vals:
        if required:
            raise error_cls(f"{what}: missing required {lower_name} field")
        return None
    return vals[0]


def parse_http_response_head(head: bytes, *, offset: int) -> HttpHead:
    """Parse and validate an HTTP/1.0 or HTTP/1.1 response header section.

    `head` must be the complete header section including the final
    CRLF CRLF.  Transfer-Encoding and Content-Encoding are not supported;
    exactly one Content-Length is required.
    """
    if not head.endswith(HEADER_END):
        raise HttpFormatError(f"HTTP header section at offset {offset} is incomplete")
    lines = head[: -len(HEADER_END)].split(b"\r\n")
    status_line = _decode_text(lines[0], what="HTTP status line")
    m = _STATUS_LINE_RE.match(status_line)
    if not m:
        raise HttpFormatError(
            f"malformed HTTP status line {status_line!r} at offset {offset}"
        )
    version = f"{m.group(1)}.{m.group(2)}"
    if version not in ("1.0", "1.1"):
        raise HttpFormatError(
            f"unsupported HTTP version {version!r} at offset {offset} "
            f"(only 1.0 and 1.1)"
        )
    headers = _parse_header_lines(
        b"\r\n".join(lines[1:]), offset=offset, error_cls=HttpFormatError,
        what="HTTP header section",
    )
    for f in headers:
        if f.lower == "transfer-encoding":
            raise HttpFormatError(
                f"Transfer-Encoding ({f.value!r}) is not supported at "
                f"offset {offset}: responses must be a plain Content-Length body"
            )
        if f.lower == "content-encoding":
            raise HttpFormatError(
                f"Content-Encoding ({f.value!r}) is not supported at "
                f"offset {offset}: compressed responses are out of scope"
            )
    cl = _singleton(headers, "content-length", required=True,
                    error_cls=HttpFormatError,
                    what=f"HTTP header section at offset {offset}")
    try:
        content_length = parse_length_field(cl, field_name="HTTP Content-Length")
    except WarcFormatError as exc:
        raise HttpFormatError(str(exc)) from None
    return HttpHead(
        version=version,
        status=int(m.group(3)),
        reason=m.group(4) or "",
        headers=headers,
        head_len=len(head),
        content_length=content_length,
    )


# --------------------------------------------------------------------------
# the streaming parser
# --------------------------------------------------------------------------

class WarcParser:
    """Parse records from a read()/tell() stream, one at a time."""

    def __init__(self, stream, *, limits: Limits = Limits()):
        self._s = stream
        self._lim = limits
        self._buf = bytearray()
        self._eof = False
        self._base = 0                # absolute offset of self._buf[0]
        self._hashers: list = []      # updated by _take for consumed bytes

    # -- buffered primitives ------------------------------------------------

    def _fill(self, n: int) -> None:
        """Ensure at least n buffered bytes (or EOF)."""
        while not self._eof and len(self._buf) < n:
            data = self._s.read(self._lim.read_chunk)
            if not data:
                self._eof = True
            else:
                self._buf += data

    def _take(self, n: int) -> bytes:
        data = bytes(self._buf[:n])
        del self._buf[:n]
        self._base += n
        for h in self._hashers:
            h.update(data)
        return data

    def _take_exact(self, n: int, what: str) -> bytes:
        self._fill(n)
        if len(self._buf) < n:
            raise WarcTruncatedError(
                f"unexpected end of stream at offset {self._base} while "
                f"reading {what}: need {n} more byte(s), got {len(self._buf)}"
            )
        return self._take(n)

    def _take_until(self, delim: bytes, limit: int, what: str,
                    span_left: int | None = None) -> bytes:
        """Consume through the next occurrence of `delim`.

        At most `limit` bytes (including the delimiter) may be consumed;
        `span_left` additionally bounds consumption to the remainder of an
        enclosing content block.  The delimiter may straddle read chunks.
        """
        search_from = 0
        while True:
            i = self._buf.find(delim, search_from)
            if i >= 0:
                n = i + len(delim)
                if span_left is not None and n > span_left:
                    raise WarcFormatError(
                        f"{what} at offset {self._base} does not end within "
                        f"its WARC content block"
                    )
                if n > limit:
                    raise WarcLimitError(
                        f"{what} at offset {self._base} exceeds the "
                        f"{limit}-byte limit"
                    )
                return self._take(n)
            search_from = max(0, len(self._buf) - (len(delim) - 1))
            cap = limit if span_left is None else min(limit, span_left)
            if len(self._buf) >= cap:
                if span_left is not None and span_left <= limit:
                    raise WarcFormatError(
                        f"{what} at offset {self._base} does not end within "
                        f"its WARC content block"
                    )
                raise WarcLimitError(
                    f"{what} at offset {self._base} exceeds the "
                    f"{limit}-byte limit"
                )
            if self._eof:
                raise WarcTruncatedError(
                    f"unexpected end of stream at offset {self._base + len(self._buf)} "
                    f"while reading {what}"
                )
            self._fill(len(self._buf) + self._lim.read_chunk)

    # -- record level --------------------------------------------------------

    def records(self):
        """Yield ParsedRecord objects until a clean end of stream."""
        seq = 0
        while True:
            self._fill(1)
            if not self._buf:           # clean EOF between records
                return
            yield self._parse_record(seq)
            seq += 1

    def _parse_record(self, seq: int) -> ParsedRecord:
        lim = self._lim
        record_offset = self._base
        record_h = hashlib.sha256()
        self._hashers = [record_h]
        try:
            version = self._take_exact(len(VERSION_LINE), "record version line")
            if version != VERSION_LINE:
                raise WarcFormatError(
                    f"record #{seq} at offset {record_offset} does not start "
                    f"with {VERSION_LINE!r}"
                )
            head = self._take_until(HEADER_END, lim.max_header_bytes,
                                    "WARC header section")
            warc_headers = _parse_header_lines(
                head[: -len(HEADER_END)], offset=record_offset,
                error_cls=WarcFormatError, what="WARC header section",
            )
            for name in _SINGLETON_FIELDS:
                # duplicate identity/boundary fields are rejected here
                _singleton(warc_headers, name, required=False,
                           error_cls=WarcFormatError,
                           what=f"WARC header section at offset {record_offset}")

            def sfield(name, required=True):
                return _singleton(warc_headers, name, required=required,
                                  error_cls=WarcFormatError,
                                  what=f"WARC header section at offset {record_offset}")

            record_id = sfield("warc-record-id")
            if not record_id:
                raise WarcFormatError(
                    f"WARC header section at offset {record_offset}: "
                    f"empty WARC-Record-ID"
                )
            record_type = sfield("warc-type").lower()
            if record_type not in ("response", "revisit"):
                raise UnsupportedRecordTypeError(
                    f"record {record_id!r} at offset {record_offset}: "
                    f"unsupported WARC-Type {record_type!r} "
                    f"(only 'response' and 'revisit')"
                )
            date_utc, date_us = parse_instant(sfield("warc-date"))
            target_uri = sfield("warc-target-uri")
            content_length = parse_length_field(sfield("content-length"))
            if content_length > lim.max_block_bytes:
                raise WarcLimitError(
                    f"record {record_id!r} at offset {record_offset}: "
                    f"Content-Length {content_length} exceeds the "
                    f"{lim.max_block_bytes}-byte block limit"
                )
            block_digest = parse_digest_field(sfield("warc-block-digest"),
                                              field_name="WARC-Block-Digest")
            payload_digest = parse_digest_field(sfield("warc-payload-digest"),
                                                field_name="WARC-Payload-Digest")
            refers_to = None
            if record_type == "revisit":
                profile = sfield("warc-profile")
                if profile not in REVISIT_PROFILES:
                    raise UnsupportedRecordTypeError(
                        f"revisit record {record_id!r} at offset {record_offset}: "
                        f"unsupported WARC-Profile {profile!r} (only "
                        f"identical-payload-digest)"
                    )
                refers_to = sfield("warc-refers-to")
                if not refers_to:
                    raise WarcFormatError(
                        f"revisit record {record_id!r} at offset {record_offset}: "
                        f"empty WARC-Refers-To"
                    )

            content_offset = self._base
            block_h = hashlib.sha256()
            self._hashers.append(block_h)
            cursor = _ContentCursor(self, content_length)
            http_head_bytes = cursor.take_until(
                HEADER_END, lim.max_header_bytes, "HTTP header section"
            )
            http = parse_http_response_head(http_head_bytes, offset=content_offset)
            if record_type == "response":
                body_length = cursor.remaining
                if body_length != http.content_length:
                    raise HttpFormatError(
                        f"record {record_id!r} at offset {record_offset}: HTTP "
                        f"Content-Length {http.content_length} != actual body "
                        f"length {body_length} in the content block"
                    )
                payload_h = hashlib.sha256()
                self._hashers.append(payload_h)
                cursor.drain()
                self._hashers.remove(payload_h)
                body_offset = content_offset + http.head_len
                computed_payload = payload_h.hexdigest()
            else:  # revisit: the block is exactly the HTTP header section
                if cursor.remaining != 0:
                    raise HttpFormatError(
                        f"revisit record {record_id!r} at offset {record_offset}: "
                        f"content block holds {cursor.remaining} byte(s) beyond "
                        f"the HTTP header section; a revisit block carries no body"
                    )
                body_offset = None
                body_length = None
                computed_payload = None
            self._hashers.remove(block_h)

            separator = self._take_exact(len(RECORD_SEPARATOR), "record separator")
            if separator != RECORD_SEPARATOR:
                raise WarcFormatError(
                    f"record {record_id!r} at offset {record_offset} is not "
                    f"followed by the CRLF CRLF record separator"
                )
        finally:
            self._hashers = []

        computed_block = block_h.hexdigest()
        if computed_block != block_digest:
            raise DigestMismatchError(
                f"record {record_id!r} at offset {record_offset}: "
                f"WARC-Block-Digest {block_digest} != computed {computed_block}"
            )
        if computed_payload is not None and computed_payload != payload_digest:
            raise DigestMismatchError(
                f"record {record_id!r} at offset {record_offset}: "
                f"WARC-Payload-Digest {payload_digest} != computed "
                f"{computed_payload}"
            )
        return ParsedRecord(
            record_id=record_id,
            record_type=record_type,
            target_uri=target_uri,
            date_utc=date_utc,
            date_us=date_us,
            refers_to=refers_to,
            block_digest=block_digest,
            payload_digest=payload_digest,
            http=http,
            record_offset=record_offset,
            record_length=self._base - record_offset,
            record_sha256=record_h.hexdigest(),
            content_offset=content_offset,
            content_length=content_length,
            body_offset=body_offset,
            body_length=body_length,
            warc_headers=warc_headers,
        )


class _ContentCursor:
    """Bounded reader over one WARC content block."""

    def __init__(self, parser: WarcParser, length: int):
        self._p = parser
        self.remaining = length

    def take_until(self, delim: bytes, limit: int, what: str) -> bytes:
        data = self._p._take_until(delim, limit, what, span_left=self.remaining)
        self.remaining -= len(data)
        return data

    def drain(self) -> None:
        """Consume the rest of the block (bytes flow into active hashers)."""
        while self.remaining:
            n = min(self.remaining, self._p._lim.read_chunk)
            self._p._take_exact(n, "content block")
            self.remaining -= n
