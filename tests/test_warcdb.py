"""Regression tests for warcdb.  Run: python3 -m pytest tests/ -q"""

from __future__ import annotations

import hashlib
import io
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from archivestore import ArchiveStore, generate_key_file  # noqa: E402
from archivestore.errors import AuthenticationError, TargetExistsError  # noqa: E402
from warcdb import FactDB, FactStore, Limits, fixtures  # noqa: E402
from warcdb.errors import (  # noqa: E402
    DigestMismatchError,
    ExportVerificationError,
    FetchNotResolvedError,
    HttpFormatError,
    ImportConflictError,
    RecordNotFoundError,
    UnsupportedDigestError,
    UnsupportedRecordTypeError,
    WarcFormatError,
    WarcLimitError,
    WarcTruncatedError,
)
from warcdb.warc import WarcParser, parse_instant  # noqa: E402

RID = "<urn:uuid:00000000-0000-4000-8000-{:012d}>"
URI = "http://example.org/page"
DATE = "2026-11-10T08:00:00Z"
BODY = b"body \x00 bytes\r\n with CRLF and\r\n\r\nWARC/1.1\r\nfake: 1\r\n\r\n"


def rid(n: int) -> str:
    return RID.format(n)


def parse(blob: bytes, **limit_kw):
    limits = Limits(**limit_kw) if limit_kw else Limits()
    return list(WarcParser(io.BytesIO(blob), limits=limits).records())


def resp(n, body=BODY, **kw):
    return fixtures.response_record(rid(n), URI, DATE, body, **kw)


def rev(n, ref, body=BODY, **kw):
    return fixtures.revisit_record(rid(n), URI, DATE, ref, len(body),
                                   fixtures.payload_digest_hex(body), **kw)


def raw_record(field_lines, block=b""):
    head = "".join(f + "\r\n" for f in field_lines).encode()
    return b"WARC/1.1\r\n" + head + b"\r\n" + block + b"\r\n\r\n"


def valid_fields(record_id="<urn:uuid:t>", block=b"", payload=b"",
                 rtype="response", extra=()):
    return [
        f"WARC-Record-ID: {record_id}",
        f"WARC-Type: {rtype}",
        f"WARC-Date: {DATE}",
        f"WARC-Target-URI: {URI}",
        f"WARC-Block-Digest: sha256:{hashlib.sha256(block).hexdigest()}",
        f"WARC-Payload-Digest: sha256:{hashlib.sha256(payload).hexdigest()}",
        f"Content-Length: {len(block)}",
        *extra,
    ]


def http_block(body=b"hi", status_line="HTTP/1.1 200 OK", headers=()):
    lines = [status_line, f"Content-Length: {len(body)}",
             *[f"{n}: {v}" for n, v in headers]]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


# ---------------------------------------------------------------------------
# parser: happy paths
# ---------------------------------------------------------------------------

def test_parse_response_offsets_and_digests():
    rec = resp(1)
    (r,) = parse(fixtures.assemble(rec))
    assert r.record_id == rid(1) and r.record_type == "response"
    assert r.record_offset == 0 and r.record_length == len(rec.data)
    assert r.record_sha256 == rec.record_sha256
    assert r.body_offset == rec.body_offset and r.body_length == len(BODY)
    assert r.payload_digest == fixtures.payload_digest_hex(BODY)
    assert r.http.status == 200 and r.http.version == "1.1"
    assert r.http.content_length == len(BODY)


def test_parse_multi_record_archive():
    recs = [resp(1), rev(2, rid(1)), rev(3, rid(2))]
    blob = fixtures.assemble(*recs)
    parsed = parse(blob)
    assert [p.record_id for p in parsed] == [rid(1), rid(2), rid(3)]
    assert [p.record_offset for p in parsed] == [r.offset for r in recs]
    assert parsed[1].refers_to == rid(1) and parsed[2].refers_to == rid(2)
    assert parsed[1].body_offset is None and parsed[1].body_length is None


def test_header_names_case_insensitive_values_preserved():
    block = http_block(b"hi", headers=[("X-A", "1"), ("X-A", "2")])
    fields = [
        "warc-record-id: <urn:uuid:x>",
        "WARC-TYPE: response",
        "Warc-Date: 2026-11-10T08:00:00Z",
        "WARC-TARGET-URI: http://CaseSensitive/Path",
        f"WARC-Block-Digest: sha256:{hashlib.sha256(block).hexdigest()}",
        f"WARC-Payload-Digest: sha256:{hashlib.sha256(b'hi').hexdigest()}",
        f"content-length: {len(block)}",
        "X-Custom: first",
        "X-Custom: second",
    ]
    (r,) = parse(raw_record(fields, block))
    assert r.record_id == "<urn:uuid:x>"
    assert r.target_uri == "http://CaseSensitive/Path"  # verbatim business key
    customs = [f.value for f in r.warc_headers if f.lower == "x-custom"]
    assert customs == ["first", "second"]               # duplicates preserved
    xa = [f.value for f in r.http.headers if f.lower == "x-a"]
    assert xa == ["1", "2"]


@pytest.mark.parametrize("chunk", [1, 2, 3, 5, 7, 64, 65536])
def test_read_chunk_size_does_not_change_results(chunk):
    recs = [resp(1), rev(2, rid(1)), resp(3, body=os.urandom(500))]
    blob = fixtures.assemble(*recs)
    want = parse(blob, read_chunk=65536)
    got = parse(blob, read_chunk=chunk)
    assert want == got


def test_body_with_fake_record_bytes_keeps_boundaries():
    body = (b"\r\n\r\nWARC/1.1\r\nWARC-Type: response\r\nContent-Length: 3\r\n"
            b"\r\nAAA\r\n\r\n" + b"\x00" * 100)
    rec = resp(1, body=body)
    blob = fixtures.assemble(rec, rev(2, rid(1), body=body))
    parsed = parse(blob)
    assert len(parsed) == 2
    assert parsed[0].body_length == len(body)
    assert parsed[0].payload_digest == fixtures.payload_digest_hex(body)


def test_http_10_response_accepted():
    rec = resp(1, http_version="1.0")
    (r,) = parse(fixtures.assemble(rec))
    assert r.http.version == "1.0"


def test_date_with_offset_normalised_to_utc():
    rec = fixtures.response_record(rid(1), URI, "2026-11-10T10:30:00+02:30", BODY)
    (r,) = parse(fixtures.assemble(rec))
    assert r.date_utc == "2026-11-10T08:00:00.000000+00:00"
    _, us = parse_instant("2026-11-10T08:00:00Z")
    assert r.date_us == us


# ---------------------------------------------------------------------------
# parser: rejections with precise diagnostics
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", [
    "WARC-Record-ID: <urn:uuid:a>",
    "WARC-Type: response",
    "WARC-Date: 2026-11-10T08:00:00Z",
    "WARC-Target-URI: http://x/",
])
def test_duplicate_identity_fields_rejected(field):
    block = http_block(b"hi")
    fields = valid_fields(block=block, payload=b"hi")
    fields.insert(1, field)  # second occurrence of an identity field
    if field.startswith("WARC-Target-URI"):
        fields.insert(1, "WARC-Target-URI: http://y/")
    with pytest.raises(WarcFormatError, match="duplicate"):
        parse(raw_record(fields, block))


def test_duplicate_content_length_rejected():
    block = http_block(b"hi")
    fields = valid_fields(block=block, payload=b"hi")
    fields.append(f"Content-Length: {len(block)}")
    with pytest.raises(WarcFormatError, match="duplicate"):
        parse(raw_record(fields, block))


def test_duplicate_digest_fields_rejected():
    block = http_block(b"hi")
    fields = valid_fields(block=block, payload=b"hi")
    fields.append(f"WARC-Block-Digest: sha256:{hashlib.sha256(block).hexdigest()}")
    with pytest.raises(WarcFormatError, match="duplicate"):
        parse(raw_record(fields, block))


@pytest.mark.parametrize("bad", ["abc", "-1", "1.5", "12x", "+5", "1 2", ""])
def test_illegal_content_length_rejected(bad):
    block = http_block(b"hi")
    fields = [f for f in valid_fields(block=block, payload=b"hi")
              if not f.startswith("Content-Length")]
    fields.append(f"Content-Length: {bad}")
    with pytest.raises(WarcFormatError, match="illegal Content-Length"):
        parse(raw_record(fields, block))


def test_missing_content_length_rejected():
    block = http_block(b"hi")
    fields = [f for f in valid_fields(block=block, payload=b"hi")
              if not f.startswith("Content-Length")]
    with pytest.raises(WarcFormatError, match="missing required content-length"):
        parse(raw_record(fields, block))


def test_block_limit():
    rec = resp(1)
    blob = fixtures.assemble(rec)
    with pytest.raises(WarcLimitError, match="block limit"):
        parse(blob, max_block_bytes=10)


def test_header_limit():
    rec = resp(1, warc_extra=[("X-Pad", "y" * 512)])
    blob = fixtures.assemble(rec)
    with pytest.raises(WarcLimitError):
        parse(blob, max_header_bytes=256)
    # and the same archive parses with a sufficient limit
    assert len(parse(blob, max_header_bytes=4096)) == 1


@pytest.mark.parametrize("cut", [5, 50, 200, -4, -1])
def test_truncation_detected(cut):
    blob = fixtures.assemble(resp(1), rev(2, rid(1)))
    frag = blob[:cut] if cut > 0 else blob[:cut]
    with pytest.raises(WarcTruncatedError):
        parse(frag)


def test_truncation_inside_body_detected():
    blob = fixtures.assemble(resp(1))
    with pytest.raises(WarcTruncatedError):
        parse(blob[:-10])


def test_trailing_garbage_rejected_no_resync():
    blob = fixtures.assemble(resp(1)) + b"WARC/1.1\r\nJUNK\r\n\r\n"
    with pytest.raises(WarcFormatError):
        parse(blob)


def test_garbage_between_records_rejected():
    blob = fixtures.assemble(resp(1)) + b"junk" + fixtures.assemble(resp(2))
    with pytest.raises(WarcFormatError):
        parse(blob)


def test_empty_stream_yields_no_records():
    assert parse(b"") == []


def test_unsupported_record_type():
    block = b"some metadata"
    fields = valid_fields(rtype="warcinfo", block=block)
    with pytest.raises(UnsupportedRecordTypeError, match="warcinfo"):
        parse(raw_record(fields, block))


def test_unsupported_revisit_profile():
    rec = rev(1, rid(2), profile="http://netpreserve.org/warc/1.1/revisit/server-not-modified")
    with pytest.raises(UnsupportedRecordTypeError, match="WARC-Profile"):
        parse(fixtures.assemble(rec))


def test_date_without_timezone_rejected():
    rec = fixtures.response_record(rid(1), URI, "2026-11-10T08:00:00", BODY)
    with pytest.raises(WarcFormatError, match="no timezone"):
        parse(fixtures.assemble(rec))


def test_bad_date_rejected():
    rec = fixtures.response_record(rid(1), URI, "not-a-date", BODY)
    with pytest.raises(WarcFormatError, match="ISO-8601"):
        parse(fixtures.assemble(rec))


def test_non_sha256_digest_rejected():
    block = http_block(b"hi")
    fields = valid_fields(block=block, payload=b"hi")
    fields[4] = f"WARC-Block-Digest: sha1:{'0' * 40}"
    with pytest.raises(UnsupportedDigestError):
        parse(raw_record(fields, block))


def test_bad_digest_hex_rejected():
    block = http_block(b"hi")
    fields = valid_fields(block=block, payload=b"hi")
    fields[4] = "WARC-Block-Digest: sha256:zzzz"
    with pytest.raises(WarcFormatError, match="hex"):
        parse(raw_record(fields, block))


def test_block_digest_mismatch():
    rec = resp(1)
    blob = bytearray(fixtures.assemble(rec))
    blob[rec.body_offset] ^= 0x01   # flip a byte inside the body
    with pytest.raises(DigestMismatchError, match="Block-Digest"):
        parse(bytes(blob))


def test_payload_digest_mismatch():
    rec = resp(1, payload_digest=fixtures.payload_digest_hex(b"other"))
    with pytest.raises(DigestMismatchError, match="Payload-Digest"):
        parse(fixtures.assemble(rec))


# ---------------------------------------------------------------------------
# parser: HTTP-level constraints
# ---------------------------------------------------------------------------

def test_chunked_transfer_rejected():
    block = http_block(b"hi", headers=[("Transfer-Encoding", "chunked")])
    fields = valid_fields(block=block, payload=b"hi")
    with pytest.raises(HttpFormatError, match="Transfer-Encoding"):
        parse(raw_record(fields, block))


def test_content_encoding_rejected():
    block = http_block(b"hi", headers=[("Content-Encoding", "gzip")])
    fields = valid_fields(block=block, payload=b"hi")
    with pytest.raises(HttpFormatError, match="Content-Encoding"):
        parse(raw_record(fields, block))


def test_http2_rejected():
    block = http_block(b"hi", status_line="HTTP/2 200 OK")
    fields = valid_fields(block=block, payload=b"hi")
    with pytest.raises(HttpFormatError):
        parse(raw_record(fields, block))


def test_http_declared_length_mismatch():
    rec = resp(1, declared_length=len(BODY) + 5)
    with pytest.raises(HttpFormatError, match="actual body length"):
        parse(fixtures.assemble(rec))


def test_http_duplicate_content_length_rejected():
    head = (b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n")
    block = head + b"hi"
    fields = valid_fields(block=block, payload=b"hi")
    with pytest.raises(HttpFormatError, match="duplicate"):
        parse(raw_record(fields, block))


def test_revisit_block_with_body_bytes_rejected():
    rec = rev(1, rid(2), trailing_bytes=b"EXTRA")
    with pytest.raises(HttpFormatError, match="no body"):
        parse(fixtures.assemble(rec))


def test_revisit_missing_http_content_length_rejected():
    head = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n"
    fields = valid_fields(rtype="revisit", block=head,
                          extra=["WARC-Profile: " + fixtures.PROFILE_11,
                                 "WARC-Refers-To: <urn:uuid:zz>"])
    with pytest.raises(HttpFormatError, match="content-length"):
        parse(raw_record(fields, head))


def test_folded_header_rejected():
    block = http_block(b"hi")
    fields = valid_fields(block=block, payload=b"hi")
    fields.append(" folded-continuation")
    with pytest.raises(WarcFormatError, match="folded"):
        parse(raw_record(fields, block))


def test_non_utf8_header_rejected():
    block = http_block(b"hi")
    head = b"WARC-Record-ID: <urn:uuid:t>\r\nX-Bad: \xff\xfe\r\n"
    blob = (b"WARC/1.1\r\n" + head +
            "".join(f + "\r\n" for f in valid_fields(block=block, payload=b"hi")
                    if not f.startswith("WARC-Record-ID")).encode() +
            b"\r\n" + block + b"\r\n\r\n")
    with pytest.raises(WarcFormatError, match="UTF-8"):
        parse(blob)


# ---------------------------------------------------------------------------
# FactStore over a real ArchiveStore
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path):
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()
    k1 = keys_dir / "k1.key"
    k2 = keys_dir / "k2.key"
    generate_key_file(k1)
    generate_key_file(k2)
    storage = tmp_path / "storage"
    store = ArchiveStore(storage, {"k1": k1, "k2": k2})
    db = FactDB(tmp_path / "facts.db")
    facts = FactStore(store, db)

    def seal_records(name, *records, block_size=256):
        path = tmp_path / f"{name}.warc"
        path.write_bytes(fixtures.assemble(*records))
        return store.seal(path, key_id="k1", block_size=block_size)

    yield tmp_path, store, db, facts, k1, k2, seal_records
    db.close()
    store.close()


def test_pending_then_resolved_across_archives(env):
    _, _, db, facts, *_ , seal = env[0], env[1], env[2], env[3], env[6]
    a1 = seal("w1", rev(2, rid(1)))
    r = facts.import_archive(a1)
    assert r.new_fetches == 1
    assert db.get(rid(2)).status == "pending"
    a2 = seal("w2", resp(1))
    facts.import_archive(a2)
    f = db.get(rid(2))
    assert f.status == "resolved"
    assert f.source_archive_id == a2
    assert f.endpoint_id == rid(1)


def test_cycle_detected(env):
    _, _, db, facts, *_ = env[0], env[1], env[2], env[3]
    seal = env[6]
    a = seal("w", rev(1, rid(2)), rev(2, rid(1)))
    facts.import_archive(a)
    assert db.get(rid(1)).status == "error"
    assert "cycle" in db.get(rid(1)).status_detail
    assert db.get(rid(2)).status == "error"


def test_resolution_digest_and_length_mismatch(env):
    _, _, db, facts, *_ = env[0], env[1], env[2], env[3]
    seal = env[6]
    bad_dig = fixtures.revisit_record(rid(2), URI, DATE, rid(1), len(BODY),
                                      fixtures.payload_digest_hex(b"WRONG"))
    bad_len = fixtures.revisit_record(rid(3), URI, DATE, rid(1), len(BODY) + 1,
                                      fixtures.payload_digest_hex(BODY))
    facts.import_archive(seal("w1", resp(1)))
    facts.import_archive(seal("w2", bad_dig, bad_len))
    assert "digest mismatch" in db.get(rid(2)).status_detail
    assert "length" in db.get(rid(3)).status_detail


def test_duplicate_and_conflict_semantics(env):
    _, _, db, facts, *_ = env[0], env[1], env[2], env[3]
    seal = env[6]
    a1 = seal("w1", resp(1))
    facts.import_archive(a1)
    # identical bytes again: duplicate, no new fetch
    a2 = seal("w1again", resp(1))
    r = facts.import_archive(a2)
    assert r.duplicates == 1 and r.new_fetches == 0
    assert db.stats()["fetches"] == 1
    # same Record-ID, different bytes: conflict
    other = fixtures.response_record(rid(1), URI, "2026-11-11T00:00:00Z", BODY)
    a3 = seal("w2", other)
    with pytest.raises(ImportConflictError):
        facts.import_archive(a3)
    assert db.stats()["fetches"] == 1
    assert db.get(rid(1)).date_utc == "2026-11-10T08:00:00.000000+00:00"
    # re-import of a known archive is an idempotent confirmation
    r = facts.import_archive(a1)
    assert r.already_imported and r.new_fetches == 0


def test_duplicates_within_one_archive(env):
    _, _, db, facts, *_ = env[0], env[1], env[2], env[3]
    seal = env[6]
    # the same record twice in one archive: one fetch, one duplicate
    a = seal("w", resp(1), resp(1))
    r = facts.import_archive(a)
    assert r.records_total == 2 and r.new_fetches == 1 and r.duplicates == 1
    assert db.stats()["fetches"] == 1
    # same Record-ID twice but with different bytes: conflict
    other = fixtures.response_record(rid(2), URI, "2026-11-11T00:00:00Z", BODY)
    a2 = seal("w2", resp(2), other)
    with pytest.raises(ImportConflictError):
        facts.import_archive(a2)
    assert db.stats()["fetches"] == 1


def test_failed_import_leaves_no_rows(env):
    _, _, db, facts, *_ = env[0], env[1], env[2], env[3]
    seal = env[6]
    good = resp(1)
    bad = resp(2, payload_digest=fixtures.payload_digest_hex(b"WRONG"))
    a = seal("w", good, bad)
    with pytest.raises(DigestMismatchError):
        facts.import_archive(a)
    assert db.stats()["fetches"] == 0
    assert db.get(rid(1)) is None


def test_rollback_on_pre_commit_failure(env):
    _, _, db, facts, *_ = env[0], env[1], env[2], env[3]
    seal = env[6]
    a = seal("w", resp(1), rev(2, rid(1)))

    def boom(stage):
        raise RuntimeError("simulated crash before commit")

    with pytest.raises(RuntimeError):
        facts.import_archive(a, pause_hook=boom)
    assert db.stats()["fetches"] == 0
    # recovery: the same archive imports cleanly afterwards
    r = facts.import_archive(a)
    assert r.new_fetches == 2
    assert db.get(rid(2)).status == "resolved"


def test_lookup_rules(env):
    _, _, db, facts, *_ = env[0], env[1], env[2], env[3]
    seal = env[6]
    d1 = fixtures.response_record(rid(1), URI, "2026-11-10T08:00:00Z", BODY)
    d2 = fixtures.revisit_record(rid(2), URI, "2026-11-12T10:00:00+02:00",
                                 rid(1), len(BODY), fixtures.payload_digest_hex(BODY))
    d3 = fixtures.revisit_record(rid(3), URI, "2026-11-12T08:00:00Z",
                                 rid(1), len(BODY), fixtures.payload_digest_hex(BODY))
    d4 = fixtures.revisit_record(rid(4), URI, "2026-11-13T08:00:00Z",
                                 rid(99), len(BODY), fixtures.payload_digest_hex(BODY))
    facts.import_archive(seal("w", d1, d2, d3, d4))

    def at(s):
        return parse_instant(s)[1]

    # tie at the same instant: lexicographically greatest Record-ID wins
    hit = db.lookup(URI, at("2026-11-12T08:00:00Z"))
    assert hit["fetch"]["record_id"] == rid(3)
    # latest overall is the pending one and is reported honestly
    hit = db.lookup(URI, at("2026-11-14T00:00:00Z"))
    assert hit["fetch"]["record_id"] == rid(4)
    assert hit["fetch"]["status"] == "pending" and hit["source"] is None
    # earlier cutoff, unknown URI
    assert db.lookup(URI, at("2026-11-10T07:00:00Z"))["found"] is False
    assert db.lookup("http://nobody/", at("2026-11-14T00:00:00Z"))["found"] is False


def test_export_success_and_guards(env):
    tmp_path, store, db, facts = env[0], env[1], env[2], env[3]
    k1, k2, seal = env[4], env[5], env[6]
    a = seal("w", resp(1), rev(2, rid(1)))
    facts.import_archive(a)
    out = tmp_path / "body.bin"
    res = facts.export_body(rid(2), out)
    assert out.read_bytes() == BODY
    assert res.bytes_written == len(BODY)
    # refuses to overwrite
    with pytest.raises(TargetExistsError):
        facts.export_body(rid(2), out)
    assert out.read_bytes() == BODY
    # unknown / unresolved fetches
    with pytest.raises(RecordNotFoundError):
        facts.export_body(rid(42), tmp_path / "x.bin")
    a2 = seal("w2", rev(3, rid(99)))
    facts.import_archive(a2)
    with pytest.raises(FetchNotResolvedError):
        facts.export_body(rid(3), tmp_path / "y.bin")
    assert not (tmp_path / "y.bin").exists()


def test_export_detects_wrong_key_and_rotation_keeps_facts(env):
    tmp_path, store, db, facts = env[0], env[1], env[2], env[3]
    k1, k2, seal = env[4], env[5], env[6]
    a = seal("w", resp(1))
    facts.import_archive(a)
    # rotate k1 -> k2; the fact DB needs no change at all
    assert store.rotate(old_key_id="k1", new_key_id="k2") == 1
    store2 = ArchiveStore(tmp_path / "storage", {"k2": k2})
    facts2 = FactStore(store2, db)
    out = tmp_path / "post-rotate.bin"
    facts2.export_body(rid(1), out)
    assert out.read_bytes() == BODY
    store2.close()
    # a wrong key fails authentication and leaves no result file
    k_wrong = tmp_path / "keys" / "wrong.key"
    generate_key_file(k_wrong)
    store3 = ArchiveStore(tmp_path / "storage", {"k2": k_wrong})
    facts3 = FactStore(store3, db)
    with pytest.raises(AuthenticationError):
        facts3.export_body(rid(1), tmp_path / "bad.bin")
    assert not (tmp_path / "bad.bin").exists()
    store3.close()


def test_export_verification_failure_leaves_no_file(env, monkeypatch):
    tmp_path, store, db, facts = env[0], env[1], env[2], env[3]
    seal = env[6]
    a = seal("w", resp(1))
    facts.import_archive(a)
    # corrupt the stored source range: point 1 byte past the real body
    db._con.execute("UPDATE fetches SET source_offset = source_offset + 1"
                    " WHERE record_id = ?", (rid(1),))
    with pytest.raises(ExportVerificationError):
        facts.export_body(rid(1), tmp_path / "v.bin")
    assert not (tmp_path / "v.bin").exists()


def test_each_fetch_keeps_its_own_headers(env):
    _, _, db, facts, *_ = env[0], env[1], env[2], env[3]
    seal = env[6]
    root = fixtures.response_record(rid(1), URI, DATE, BODY, status=200,
                                    http_headers=[("X-Marker", "root")])
    top = fixtures.revisit_record(rid(2), URI, "2026-11-10T09:00:00Z", rid(1),
                                  len(BODY), fixtures.payload_digest_hex(BODY),
                                  status=200,
                                  http_headers=[("X-Marker", "top")])
    facts.import_archive(seal("w", root, top))
    f_root = db.get(rid(1))
    f_top = db.get(rid(2))
    assert [h.value for h in f_top.headers if h.name == "X-Marker"] == ["top"]
    assert [h.value for h in f_root.headers if h.name == "X-Marker"] == ["root"]
    assert f_top.date_utc != f_root.date_utc
    # the resolved source points at the root's body, the metadata stays its own
    assert f_top.source_archive_id == f_root.archive_id


def test_import_read_chunk_invariance(env):
    _, _, db, facts, *_ = env[0], env[1], env[2], env[3]
    seal = env[6]
    a = seal("w", resp(1), rev(2, rid(1)), resp(3, body=os.urandom(900)))
    facts.import_archive(a, limits=Limits(read_chunk=65536))
    dump_big = db.dump()
    db2 = FactDB(env[0] / "facts2.db")
    facts_b = FactStore(env[1], db2)
    facts_b.import_archive(a, limits=Limits(read_chunk=3))
    assert db2.dump() == dump_big
    db2.close()


def test_chain_view(env):
    _, _, db, facts, *_ = env[0], env[1], env[2], env[3]
    seal = env[6]
    a = seal("w", rev(3, rid(2)))
    facts.import_archive(a)
    with pytest.raises(RecordNotFoundError):
        db.chain(rid(99))
    facts.import_archive(seal("w2", rev(2, rid(1))))
    facts.import_archive(seal("w3", resp(1)))
    c = db.chain(rid(3))
    assert [h["record_id"] for h in c["hops"]] == [rid(3), rid(2), rid(1)]
    assert c["terminal"] == {"kind": "response", "record_id": rid(1)}
    assert c["status"] == "resolved"
    assert c["source"]["archive_id"] is not None


def test_empty_archive_imports_as_zero_records(env):
    tmp_path, store, db, facts = env[0], env[1], env[2], env[3]
    empty = tmp_path / "empty.warc"
    empty.write_bytes(b"")
    a = store.seal(empty, key_id="k1", block_size=256)
    r = facts.import_archive(a)
    assert r.records_total == 0 and r.new_fetches == 0
    assert db.stats()["imports"] == 1
    r = facts.import_archive(a)
    assert r.already_imported


def test_empty_body_roundtrip(env):
    tmp_path, _, db, facts = env[0], env[1], env[2], env[3]
    seal = env[6]
    a = seal("w", resp(1, body=b""), rev(2, rid(1), body=b""))
    facts.import_archive(a)
    assert db.get(rid(2)).status == "resolved"
    out = tmp_path / "empty-body.bin"
    res = facts.export_body(rid(2), out)
    assert out.read_bytes() == b"" and res.bytes_written == 0


def test_cli_smoke(env, tmp_path):
    from warcdb.__main__ import main as cli

    _, _, db, facts = env[0], env[1], env[2], env[3]
    k1, seal = env[4], env[6]
    a = seal("wcli", resp(1), rev(2, rid(1)))
    facts_file = env[0] / "facts.db"
    storage = env[0] / "storage"
    rc = cli(["--facts", str(facts_file), "--storage", str(storage),
              "--key", f"k1={k1}", "import", "--archive", a])
    assert rc == 0
    assert cli(["--facts", str(facts_file), "stats"]) == 0
    assert cli(["--facts", str(facts_file), "chain", "--record-id", rid(2)]) == 0
    assert cli(["--facts", str(facts_file), "lookup", "--uri", URI,
                "--at", "2026-11-14T00:00:00Z"]) == 0
    out = tmp_path / "cli-export.bin"
    assert cli(["--facts", str(facts_file), "--storage", str(storage),
                "--key", f"k1={k1}", "export", "--record-id", rid(2),
                "--out", str(out)]) == 0
    assert out.read_bytes() == BODY
    # a failing command reports and exits 1
    assert cli(["--facts", str(facts_file), "chain",
                "--record-id", "<urn:uuid:nope>"]) == 1
