"""Small offline fixture generator: build WARC/1.1 byte streams by hand.

No network access is involved anywhere; bodies are caller-supplied bytes.
The builders compute the WARC-Block-Digest / WARC-Payload-Digest fields
correctly and remember the exact byte ranges of every record, content
block and body, so tests and the demo can assert source offsets against
independent expectations.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional, Sequence

PROFILE_11 = "http://netpreserve.org/warc/1.1/revisit/identical-payload-digest"


def payload_digest_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def http_head_bytes(status: int = 200, reason: str = "OK",
                    headers: Sequence = (), *, version: str = "1.1") -> bytes:
    """Serialise an HTTP response header section (incl. final CRLF CRLF)."""
    first = f"HTTP/{version} {status}"
    if reason:
        first += f" {reason}"
    lines = [first] + [f"{n}: {v}" for n, v in headers]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")


@dataclass
class BuiltRecord:
    record_id: str
    record_type: str               # 'response' | 'revisit'
    data: bytes                    # the whole WARC record, separator included
    http_head: bytes
    body: Optional[bytes]          # response only
    payload_sha256: str
    block_sha256: str
    # absolute positions, filled in by assemble()
    offset: int = 0
    content_offset: int = 0
    body_offset: Optional[int] = None

    @property
    def record_sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def body_length(self) -> Optional[int]:
        return None if self.body is None else len(self.body)


def _warc_record(record_id: str, record_type: str, uri: str, date: str,
                 block: bytes, extra_fields: Sequence = ()) -> bytes:
    fields = [
        ("WARC-Record-ID", record_id),
        ("WARC-Type", record_type),
        ("WARC-Date", date),
        ("WARC-Target-URI", uri),
        ("Content-Type", "application/http; msgtype=response"),
        *extra_fields,
        ("WARC-Block-Digest", f"sha256:{hashlib.sha256(block).hexdigest()}"),
        ("Content-Length", str(len(block))),
    ]
    head = "".join(f"{n}: {v}\r\n" for n, v in fields).encode("utf-8")
    return b"WARC/1.1\r\n" + head + b"\r\n" + block + b"\r\n\r\n"


def response_record(record_id: str, uri: str, date: str, body: bytes, *,
                    status: int = 200, reason: str = "OK",
                    http_headers: Sequence = (),
                    warc_extra: Sequence = (),
                    http_version: str = "1.1",
                    declared_length: Optional[int] = None,
                    payload_digest: Optional[str] = None) -> BuiltRecord:
    """Build a response record whose content block is a full HTTP response.

    `declared_length` overrides the HTTP Content-Length header and
    `payload_digest` overrides the WARC-Payload-Digest field (both exist
    to build deliberately inconsistent fixtures); the actual body bytes
    are always appended in full.
    """
    headers = list(http_headers)
    if not any(n.lower() == "content-length" for n, _ in headers):
        headers.append(("Content-Length",
                        str(len(body) if declared_length is None
                            else declared_length)))
    head = http_head_bytes(status, reason, headers, version=http_version)
    block = head + body
    payload_hex = hashlib.sha256(body).hexdigest()
    claimed = payload_hex if payload_digest is None else payload_digest
    extra = list(warc_extra) + [("WARC-Payload-Digest", f"sha256:{claimed}")]
    data = _warc_record(record_id, "response", uri, date, block, extra)
    return BuiltRecord(
        record_id=record_id, record_type="response", data=data,
        http_head=head, body=body, payload_sha256=payload_hex,
        block_sha256=hashlib.sha256(block).hexdigest(),
    )


def revisit_record(record_id: str, uri: str, date: str, refers_to: str,
                   declared_length: int, payload_digest: str, *,
                   status: int = 200, reason: str = "OK",
                   http_headers: Sequence = (),
                   warc_extra: Sequence = (),
                   profile: str = PROFILE_11,
                   trailing_bytes: bytes = b"") -> BuiltRecord:
    """Build a revisit record: content block = HTTP header section only.

    `payload_digest` is the hex digest this record claims for the reused
    body; `trailing_bytes` can append junk after the header section to
    build invalid fixtures.
    """
    headers = list(http_headers)
    if not any(n.lower() == "content-length" for n, _ in headers):
        headers.append(("Content-Length", str(declared_length)))
    head = http_head_bytes(status, reason, headers)
    block = head + trailing_bytes
    extra = [
        ("WARC-Profile", profile),
        ("WARC-Refers-To", refers_to),
        *warc_extra,
        ("WARC-Payload-Digest", f"sha256:{payload_digest}"),
    ]
    data = _warc_record(record_id, "revisit", uri, date, block, extra)
    return BuiltRecord(
        record_id=record_id, record_type="revisit", data=data,
        http_head=head, body=None, payload_sha256=payload_digest,
        block_sha256=hashlib.sha256(block).hexdigest(),
    )


def assemble(*records: BuiltRecord) -> bytes:
    """Concatenate records into one archive, recording absolute offsets."""
    out = bytearray()
    for rec in records:
        rec.offset = len(out)
        block_len = len(rec.http_head) + (len(rec.body) if rec.body is not None else 0)
        rec.content_offset = rec.offset + len(rec.data) - block_len - 4
        if rec.body is not None:
            rec.body_offset = rec.content_offset + len(rec.http_head)
        out += rec.data
    return bytes(out)


def response_record_stream(record_id: str, uri: str, date: str,
                           body_path, out_path, *,
                           status: int = 200, reason: str = "OK",
                           http_headers: Sequence = (),
                           chunk: int = 1 << 20) -> BuiltRecord:
    """Streaming variant of response_record() for large bodies.

    Reads the body from `body_path` and writes the single-record archive
    to `out_path`, hashing as it goes; the body is never held in memory.
    The returned BuiltRecord has offsets for a one-record archive.
    """
    from pathlib import Path

    body_path = Path(body_path)
    body_len = body_path.stat().st_size
    headers = list(http_headers)
    if not any(n.lower() == "content-length" for n, _ in headers):
        headers.append(("Content-Length", str(body_len)))
    head = http_head_bytes(status, reason, headers)

    # Pass 1: payload digest (body only) and block digest (head + body).
    payload_h = hashlib.sha256()
    block_h = hashlib.sha256()
    block_h.update(head)
    with open(body_path, "rb") as f:
        while True:
            data = f.read(chunk)
            if not data:
                break
            payload_h.update(data)
            block_h.update(data)
    payload_hex = payload_h.hexdigest()

    fields = [
        ("WARC-Record-ID", record_id),
        ("WARC-Type", "response"),
        ("WARC-Date", date),
        ("WARC-Target-URI", uri),
        ("Content-Type", "application/http; msgtype=response"),
        ("WARC-Payload-Digest", f"sha256:{payload_hex}"),
        ("WARC-Block-Digest", f"sha256:{block_h.hexdigest()}"),
        ("Content-Length", str(len(head) + body_len)),
    ]
    warc_head = "".join(f"{n}: {v}\r\n" for n, v in fields).encode("utf-8")
    content_offset = len(b"WARC/1.1\r\n") + len(warc_head) + 2
    # Pass 2: write the record, streaming the body through.
    with open(out_path, "wb") as out:
        out.write(b"WARC/1.1\r\n")
        out.write(warc_head)
        out.write(b"\r\n")
        out.write(head)
        with open(body_path, "rb") as f:
            while True:
                data = f.read(chunk)
                if not data:
                    break
                out.write(data)
        out.write(b"\r\n\r\n")
    return BuiltRecord(
        record_id=record_id, record_type="response", data=b"",
        http_head=head, body=None, payload_sha256=payload_hex,
        block_sha256=block_h.hexdigest(),
        offset=0, content_offset=content_offset,
        body_offset=content_offset + len(head),
    )
