#!/usr/bin/env python3
"""Repeatable handover drill for formfetch (real local HTTP, no network).

Run from the repository root:

    python3 -I demo/run_formfetch_drill.py

The script starts the localhost-only export site
(:class:`formfetch.testsite.ExportSite` -- session cookie, hidden
ticket, raw-request recording, independent multipart parsing) and walks
the whole handover story with **real** HTTP requests:

  1. fetch the export page, prepare (not send) a multipart submission:
     the ordered successful-control list, target, method and enctype
     are checked against hard-coded expectations; the server must have
     seen no submit request while we only inspected;
  2. a battery of invalid modifications and bad submitter choices:
     each fails before anything is sent (server request count frozen);
  3. the full multipart export (associated controls before/after the
     form, same-named field in another form, duplicate names, readonly,
     empty string, fieldset/legend exception, nested disabled,
     multi-select, radio switch, two uploads incl. a zero-byte file and
     an unselected file control): the site's independent parse must
     match the expected ordered parameters, received files are compared
     byte by byte, the delivered data file digest is verified;
  4. a second submitter overriding to GET (query replaced, path kept);
  5. a third submitter overriding target + enctype (POST urlencoded,
     action query preserved);
  6. cookie scoping: path isolation and a Secure cookie that must never
     come back over plain HTTP;
  7. server-side failures: expired ticket (403), redirect (302),
     oversized result -- none of them may produce a result file;
  8. output protection: an existing result path is refused before any
     network I/O;
  9. malformed pages (nested form, mismatched tags, duplicate form id);
 10. CLI round trip (preview-only, full run, bad config);
 11. a 192 MiB upload in a child process: peak RSS of the sender stays
     far below the file size (no whole-file residency).

Expected ordered parameters and the upload manifest are written next to
the server's raw requests and receipts under demo/formfetch_out/, and
every assertion is mirrored into assertions.log.
"""

from __future__ import annotations

import filecmp
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from formfetch import (  # noqa: E402
    FormSession, Limits, fetch_page, prepare_submission, submit,
    TextEntry, FileEntry,
    FormStructureError, HtmlSubsetError, ModificationError,
    OutputFileError, ResponseStatusError, SizeLimitError,
    UnsupportedFeatureError,
)
from formfetch.encode import build_multipart  # noqa: E402
from formfetch.testsite import (  # noqa: E402
    DATA_FILE, DATA_SHA256, SESSION_VALUE, ExportSite,
)

OUT = REPO / "demo" / "formfetch_out"
UPLOADS = OUT / "uploads"
RESULTS = OUT / "results"
SERVER_OUT = OUT / "server"

LOG_LINES = []


def log(msg=""):
    print(msg, flush=True)
    LOG_LINES.append(msg)


CHECKS = 0


def check(cond, label):
    global CHECKS
    CHECKS += 1
    if not cond:
        log(f"FAIL: {label}")
        raise SystemExit(1)
    log(f"  ok: {label}")


def expect_error(exc_types, label, fn):
    try:
        fn()
    except exc_types as exc:
        log(f"  ok: {label} -> {type(exc).__name__}: {exc}")
        return exc
    except Exception as exc:  # noqa: BLE001
        log(f"FAIL: {label}: raised {type(exc).__name__} ({exc}) instead")
        raise SystemExit(1)
    log(f"FAIL: {label}: no error raised")
    raise SystemExit(1)


def sha256_file(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def entry_pairs(prepared):
    """(name, value) pairs of a prepared submission; files as markers."""
    out = []
    for entry in prepared.entries:
        if isinstance(entry, TextEntry):
            out.append((entry.name, entry.value))
        else:
            out.append((entry.name, ("file", entry.filename)))
    return out


def crlf(expected):
    """Multipart server view: text values arrive CRLF-normalized."""
    return [(k, v.replace("\n", "\r\n") if isinstance(v, str) else v)
            for k, v in expected]


def wire_values(expected):
    """GET/urlencoded server view: text values arrive CRLF-normalized
    and unselected file controls arrive as empty values."""
    out = []
    for key, value in expected:
        if isinstance(value, str):
            out.append((key, value.replace("\n", "\r\n")))
        elif value == ("file", ""):
            out.append((key, ""))
        else:
            raise AssertionError(
                f"unexpected file marker in a non-multipart flow: {key}")
    return out


# ---------------------------------------------------------------------------
# expected ordered parameters (hard-coded independently of the code)


EXPECTED_FLOW1 = [
    ("pre-note", "before"),            # associated control before the form
    ("ticket", "TICKET-VALID"),        # hidden ticket
    ("region", "北部 & 东部"),          # modified text
    ("ro", "a & b <x>"),               # readonly still submits, entities
    ("empty", ""),                     # empty string kept
    ("pw", "s3cr3t"),                  # password (masked in previews)
    ("agree", "yes"),                  # checkbox switched on
    ("news", "on"),                    # checkbox, default value "on"
    ("speed", "slow"),                 # radio group switched
    ("format", "csv"),                 # single select, modified
    ("priority", "mid-text"),          # no selection -> first available;
                                       # option without value -> its text
    ("kinds", "rain"),                 # multi-select, modified
    ("kinds", "rain"),                 #   same value twice, not merged
    ("kinds", "wind"),
    ("notes", "line1\nline2 & more"),  # textarea, entities + newline
    ("blank", ""),                     # empty textarea
    ("legend-note", "in-legend"),      # first-legend exception
    ("tag", "a"),                      # duplicate name, order kept
    ("tag", "b"),
    ("dupx1", "d1"),
    ("dupx2", "d2"),
    ("upload", ("file", "querylist.bin")),   # zero bytes + CRLF inside
    ("uploads", ("file", "empty.txt")),      # zero-byte file
    ("uploads", ("file", "tiny.bin")),
    ("attach", ("file", "")),                # unselected file control
    ("go", "download"),                      # the clicked submitter only
    ("post-note", "after"),            # associated control after the form
    ("ext-sel", "x1"),
]

EXPECTED_FLOW2 = [   # GET override; defaults where not modified
    ("pre-note", "before"),
    ("ticket", "TICKET-VALID"),
    ("region", "north"),
    ("ro", "a & b <x>"),
    ("empty", ""),
    ("pw", ""),
    ("agree", "yes"),
    ("news", "on"),
    ("speed", "med"),        # two checked in the page: last one wins
    ("format", "xml"),       # two selected in the page: last available
    ("priority", "mid-text"),
    ("kinds", "temp"),
    ("kinds", "wind"),
    ("notes", "line1\nline2 & more"),
    ("blank", ""),
    ("legend-note", "in-legend"),
    ("tag", "a"),
    ("tag", "b"),
    ("dupx1", "d1"),
    ("dupx2", "d2"),
    ("upload", ("file", "")),   # unselected file controls
    ("uploads", ("file", "")),
    ("attach", ("file", "")),
    ("go", "query"),
    ("post-note", "after"),
    ("ext-sel", "x1"),
]

EXPECTED_FLOW3 = [   # POST urlencoded override
    ("pre-note", "before"),
    ("ticket", "TICKET-VALID"),
    ("region", "default-region"),
    ("ro", "a & b <x>"),
    ("empty", ""),
    ("pw", ""),
    ("agree", "yes"),
    ("news", "on"),
    ("speed", "med"),
    ("format", "xml"),
    ("priority", "mid-text"),
    ("kinds", "rain"),
    ("kinds", "wind"),
    ("notes", "line1\nline2 & more"),
    ("blank", ""),
    ("legend-note", "in-legend"),
    ("tag", "a"),
    ("tag", "b"),
    ("dupx1", "d1"),
    ("dupx2", "d2"),
    ("upload", ("file", "")),
    ("uploads", ("file", "")),
    ("attach", ("file", "")),
    ("go", "urlenc"),
    ("post-note", "after"),
    ("ext-sel", "x1"),
]

EXPECTED_DEFAULT = [  # no modifications, plain click on btn-download
    ("pre-note", "before"),
    ("ticket", "TICKET-VALID"),
    ("region", "default-region"),
    ("ro", "a & b <x>"),
    ("empty", ""),
    ("pw", ""),
    ("news", "on"),
    ("speed", "med"),
    ("format", "xml"),
    ("priority", "mid-text"),
    ("kinds", "temp"),
    ("kinds", "wind"),
    ("notes", "line1\nline2 & more"),
    ("blank", ""),
    ("legend-note", "in-legend"),
    ("tag", "a"),
    ("tag", "b"),
    ("dupx1", "d1"),
    ("dupx2", "d2"),
    ("upload", ("file", "")),
    ("uploads", ("file", "")),
    ("attach", ("file", "")),
    ("go", "download"),
    ("post-note", "after"),
    ("ext-sel", "x1"),
]

QUERYLIST_BYTES = (b"# query list\r\n"
                   b"row1,\x00binary,\xff\xfe\r\n"
                   b"row2,zeros below\r\n"
                   b"\x00\x00\x00\r\n"
                   + bytes(range(256)) + b"\r\n")
TINY_BYTES = b"\x00\xff\r\nTINY" * 4
BOUNDARY = "----formfetch-drill-boundary"


# ---------------------------------------------------------------------------
# child process for the big-file step


def big_child(port, big_path, out_path):
    import resource

    session = FormSession()
    page = fetch_page(session, f"http://127.0.0.1:{port}/export")
    prepared = prepare_submission(
        page, "export-form",
        modifications={"upload": [big_path]},
        submitter_id="btn-download",
        limits=Limits(upload_bytes=768 * 1024 * 1024),
        boundary="BigBoundary0123456789abcdef")
    result = submit(session, prepared, out_path)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(f"CHILD_PEAK_RSS_KB={peak}")
    print(f"CHILD_RESULT_SHA256={result.sha256}")
    print(f"CHILD_RESULT_BYTES={result.bytes_written}")


# ---------------------------------------------------------------------------


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--big-child":
        big_child(int(sys.argv[2]), sys.argv[3], sys.argv[4])
        return

    if OUT.exists():
        shutil.rmtree(OUT)
    UPLOADS.mkdir(parents=True)
    RESULTS.mkdir(parents=True)
    SERVER_OUT.mkdir(parents=True)

    querylist = UPLOADS / "querylist.bin"
    querylist.write_bytes(QUERYLIST_BYTES)
    empty = UPLOADS / "empty.txt"
    empty.write_bytes(b"")
    tiny = UPLOADS / "tiny.bin"
    tiny.write_bytes(TINY_BYTES)

    site = ExportSite(SERVER_OUT).start()
    log(f"export site on {site.url} (localhost only)")
    try:
        run_drill(site, querylist, empty, tiny)
    finally:
        site.stop()

    (OUT / "assertions.log").write_text(
        "\n".join(LOG_LINES) + "\n", encoding="utf-8")
    log(f"ALL {CHECKS} CHECKS PASSED")
    (OUT / "assertions.log").write_text(
        "\n".join(LOG_LINES) + "\n", encoding="utf-8")


def run_drill(site, querylist, empty, tiny):
    mods_flow1 = {
        "region": "北部 & 东部",
        "pw": "s3cr3t",
        "agree": True,
        "sp-slow": True,
        "format": ["fmt-csv"],
        "kinds": ["k-rain", "k-rain2", "k-wind"],
        "upload": [str(querylist)],
        "uploads": [str(empty), str(tiny)],
    }

    # -- step 1: fetch + prepare, nothing sent yet ------------------------
    log("STEP 1: fetch page, prepare multipart submission, inspect only")
    session = FormSession()
    page = fetch_page(session, site.url + "/export")
    check(page.form_ids() == ["other-form", "export-form"],
          "both forms discovered")
    check(page.model.base_url == site.url + "/export/",
          "first <base href> wins and resolves against the page URL")
    prepared = prepare_submission(
        page, "export-form", modifications=mods_flow1,
        submitter_id="btn-download", boundary=BOUNDARY)
    check(prepared.method == "POST", "method from the form")
    check(prepared.url == site.url + "/export/delivery",
          "relative action resolved against the base URL")
    check(prepared.enctype == "multipart/form-data", "enctype from form")
    check(entry_pairs(prepared) == EXPECTED_FLOW1,
          "ordered successful-control list matches the hard-coded "
          "expectation")
    posts = [r for r in site.requests if r["method"] == "POST"]
    check(len(posts) == 0, "no submit request reached the server while "
                           "we only prepared")
    described = prepared.describe()
    check(json.dumps(described, ensure_ascii=False).find("s3cr3t") == -1,
          "password value is masked in the preview")
    pw_entry = [e for e in described["entries"] if e["name"] == "pw"][0]
    check(pw_entry["value"] == "••••••", "password shown as mask")

    # -- step 2: invalid modifications, zero network side effects --------
    log("STEP 2: invalid modifications and bad submitter choices")
    before = len(site.requests)
    bad_mods = [
        ({"no-such-control": "x"}, FormStructureError, "unknown id"),
        ({"dup": "x"}, FormStructureError, "duplicated element id"),
        ({"other-region": "x"}, ModificationError,
         "control of the other form"),
        ({"orphan": "x"}, ModificationError, "control without a form"),
        ({"fs-off": "x"}, ModificationError, "disabled by fieldset"),
        ({"disabled-field": "x"}, ModificationError, "disabled control"),
        ({"agree": "text"}, ModificationError, "string to a checkbox"),
        ({"region": True}, ModificationError, "boolean to a text input"),
        ({"region": ["a"]}, ModificationError, "list to a text input"),
        ({"format": ["fmt-none"]}, ModificationError, "disabled option"),
        ({"format": ["no-such-option"]}, ModificationError,
         "unknown option id"),
        ({"format": ["fmt-csv", "fmt-json"]}, ModificationError,
         "two options for a single select"),
        ({"kinds": ["k-old1"]}, ModificationError,
         "option disabled via optgroup"),
        ({"sp-slow": True, "sp-fast": True}, ModificationError,
         "two radios of one group set true"),
        ({"upload": [str(querylist), str(tiny)]}, ModificationError,
         "two files for a non-multiple file control"),
        ({"upload": [str(UPLOADS / "missing.bin")]}, ModificationError,
         "missing upload file"),
        ({"btn-download": "x"}, ModificationError, "button modified"),
        ({"pw": 123}, ModificationError, "unsupported value type"),
        ({"kinds": "temp"}, ModificationError, "string to a select"),
    ]
    for mods, exc_type, label in bad_mods:
        expect_error(exc_type, f"modification rejected ({label})",
                     lambda m=mods: prepare_submission(
                         page, "export-form", modifications=m,
                         submitter_id="btn-download"))
    bad_submitters = [
        ("no-such-btn", FormStructureError, "unknown submitter id"),
        ("other-btn", FormStructureError, "submitter of the other form"),
        ("region", FormStructureError, "non-button as submitter"),
        ("btn-disabled", FormStructureError, "disabled submitter"),
        ("img-btn", UnsupportedFeatureError, "image button"),
        ("btn-plain", UnsupportedFeatureError, "text/plain enctype"),
        ("btn-dialog", UnsupportedFeatureError, "dialog method"),
    ]
    for submitter, exc_type, label in bad_submitters:
        expect_error(exc_type, f"submitter rejected ({label})",
                     lambda s=submitter: prepare_submission(
                         page, "export-form", submitter_id=s))
    expect_error(FormStructureError, "unknown form id",
                 lambda: prepare_submission(page, "no-such-form"))
    expect_error(ModificationError,
                 "modification aimed at another form's control",
                 lambda: prepare_submission(
                     page, "other-form",
                     modifications={"region": "x"}))
    check(len(site.requests) == before,
          "not a single request was sent for invalid input")

    # -- step 3: the full multipart export -------------------------------
    log("STEP 3: multipart export, independent parse, byte compare")
    result = submit(session, prepared, RESULTS / "export-data.bin")
    check(result.status == 200, "submit returned 2xx")
    check(result.sha256 == DATA_SHA256,
          "delivered data file digest matches the pre-generated file")
    record = site.requests[-1]
    check(record["request_line"] == "POST /export/delivery HTTP/1.1",
          "request line recorded by the site")
    check(record["complete"] and record["declared_length"] > 0,
          "announced Content-Length fully received")
    rebuilt = build_multipart(prepared.entries, prepared.boundary)
    check(rebuilt.content_length == record["declared_length"],
          "client-computed Content-Length equals what the site received")
    check(sum(len(c) for c in rebuilt.iter_chunks())
          == rebuilt.content_length,
          "streamed bytes equal the announced Content-Length")
    cookie = record["cookie"] or ""
    check(f"sid={SESSION_VALUE}" in cookie and "wide=1" in cookie,
          "session cookie and path=/ cookie rode along")
    check("sec=" not in cookie, "Secure cookie not sent over plain HTTP")

    receipt = site.receipts[-1]
    got_parts = [(p["name"], p["value"] if p["kind"] == "text"
                  else ("file", p["filename"]))
                 for p in receipt["parts"]]
    check(got_parts == crlf(EXPECTED_FLOW1),
          "site's independent parse matches the expected ordered "
          "parameters (CRLF-normalized)")
    received = {p["filename"]: p for p in receipt["parts"]
                if p["kind"] == "file" and p["filename"]}
    for original in (querylist, empty, tiny):
        part = received[original.name]
        check(filecmp.cmp(part["saved_as"], original, shallow=False),
              f"{original.name}: received bytes identical to the upload")
        check(part["sha256"] == sha256_file(original),
              f"{original.name}: sha256 matches")
    check(received["empty.txt"]["size"] == 0,
          "zero-byte file arrived as a named, empty part")
    unselected = [p for p in receipt["parts"]
                  if p["kind"] == "file" and p["filename"] == ""]
    check(len(unselected) == 1 and unselected[0]["name"] == "attach",
          "unselected file control is its own state (empty filename)")

    # -- step 4: GET override ---------------------------------------------
    log("STEP 4: second submitter overrides to GET (query replaced)")
    page2 = fetch_page(session, site.url + "/export")
    prepared2 = prepare_submission(
        page2, "export-form",
        modifications={"region": "north", "agree": True},
        submitter_id="btn-query")
    check(prepared2.method == "GET", "formmethod=get on the submitter")
    check(prepared2.url.startswith(site.url + "/export/query?"),
          "GET target path kept")
    check("fixed" not in prepared2.url and "zap" not in prepared2.url,
          "action's original query replaced, not merged")
    check(entry_pairs(prepared2) == EXPECTED_FLOW2,
          "GET entries match the hard-coded expectation")
    masked = prepare_submission(
        page2, "export-form", modifications={"pw": "topsecret"},
        submitter_id="btn-query").describe()
    check("topsecret" not in masked["url"] and "topsecret" not in
          json.dumps(masked), "password masked even inside a GET URL")
    result2 = submit(session, prepared2, RESULTS / "export-get.bin")
    check(result2.sha256 == DATA_SHA256, "GET export delivered the data "
                                         "file")
    receipt2 = site.receipts[-1]
    check(receipt2["kind"] == "get", "site saw a GET")
    check([tuple(p) for p in receipt2["pairs"]]
          == wire_values(EXPECTED_FLOW2),
          "site parsed the rebuilt query in order")
    check("fixed" not in receipt2["raw_query"]
          and "zap" not in receipt2["raw_query"],
          "original action query never reached the wire")

    # -- step 5: POST urlencoded override ---------------------------------
    log("STEP 5: third submitter overrides target + enctype")
    page3 = fetch_page(session, site.url + "/export")
    prepared3 = prepare_submission(
        page3, "export-form",
        modifications={"kinds": ["k-rain", "k-wind"], "agree": True},
        submitter_id="btn-urlenc")
    check(prepared3.method == "POST", "method stays POST")
    check(prepared3.url == site.url + "/export/alt?src=btn",
          "formaction target, action query preserved for POST")
    check(prepared3.enctype == "application/x-www-form-urlencoded",
          "formenctype override applied")
    result3 = submit(session, prepared3, RESULTS / "export-alt.bin")
    check(result3.sha256 == DATA_SHA256, "urlencoded export delivered")
    receipt3 = site.receipts[-1]
    check(receipt3["url_query"] == "src=btn",
          "POST kept the action's query")
    check([tuple(p) for p in receipt3["pairs"]]
          == wire_values(EXPECTED_FLOW3),
          "urlencoded body parsed in order, duplicates kept")

    # -- step 6: cookie scoping --------------------------------------------
    log("STEP 6: cookie path isolation and Secure handling")
    session.get_bytes(site.url + "/other", max_bytes=1024 * 1024)
    other = site.requests[-1]
    check(other["path"] == "/other", "visited /other")
    other_cookie = other["cookie"] or ""
    check("wide=1" in other_cookie, "path=/ cookie sent to /other")
    check("sid=" not in other_cookie,
          "path=/export cookie NOT sent to /other")
    check(all("sec=" not in (r["cookie"] or "") for r in site.requests),
          "Secure cookie never returned over plain HTTP")

    # -- step 7: server-side failures produce no result --------------------
    log("STEP 7: expired ticket, redirect, oversized result")
    page_x = fetch_page(session, site.url + "/export?ticket=expired")
    prepared_x = prepare_submission(page_x, "export-form",
                                    submitter_id="btn-download")
    err = expect_error(ResponseStatusError, "expired ticket",
                       lambda: submit(session, prepared_x,
                                      RESULTS / "expired.bin"))
    check(err.status == 403, "403 surfaced")
    check(not (RESULTS / "expired.bin").exists(),
          "no result file for a rejected ticket")
    check(site.receipts[-1]["parts"], "site still recorded the attempt")

    prepared_r = prepare_submission(page, "export-form",
                                    submitter_id="btn-redir")
    count_before = len(site.requests)
    err = expect_error(ResponseStatusError, "redirect not followed",
                       lambda: submit(session, prepared_r,
                                      RESULTS / "redirect.bin"))
    check(err.status == 302 and err.location == "/export/delivery",
          "302 surfaced with its Location")
    check(len(site.requests) == count_before + 1,
          "exactly one request: no auto-follow, no resubmission")
    check(not (RESULTS / "redirect.bin").exists(), "no result for 3xx")

    prepared_b = prepare_submission(page, "export-form",
                                    submitter_id="btn-big")
    expect_error(SizeLimitError, "oversized result",
                 lambda: submit(session, prepared_b,
                                RESULTS / "toobig.bin",
                                limits=Limits(result_bytes=4096)))
    check(not (RESULTS / "toobig.bin").exists(),
          "oversized response did not become a result")
    leftovers = list(RESULTS.glob(".formfetch-*"))
    check(not leftovers, "temporary files cleaned up after failures")

    # -- step 8: existing output is protected ------------------------------
    log("STEP 8: existing output path refused before any network I/O")
    count_before = len(site.requests)
    expect_error(OutputFileError, "existing result path",
                 lambda: submit(session, prepared,
                                RESULTS / "export-data.bin"))
    check(len(site.requests) == count_before,
          "refusal happened before any request")

    # -- step 9: malformed pages -------------------------------------------
    log("STEP 9: malformed pages are rejected, nothing submitted")
    expect_error(HtmlSubsetError, "nested form",
                 lambda: fetch_page(session, site.url + "/bad/nested"))
    expect_error(HtmlSubsetError, "mismatched end tag",
                 lambda: fetch_page(session, site.url + "/bad/unclosed"))
    dup_page = fetch_page(session, site.url + "/bad/dupform")
    expect_error(FormStructureError, "duplicate form id",
                 lambda: prepare_submission(dup_page, "f"))

    # -- step 10: CLI round trip --------------------------------------------
    log("STEP 10: CLI with JSON configs")
    cli_preview = OUT / "cli-preview.json"
    cli_preview.write_text(json.dumps({
        "page_url": site.url + "/export",
        "form_id": "export-form",
        "submitter_id": "btn-download",
        "preview_only": True,
    }), encoding="utf-8")
    count_before = len(site.requests)
    proc = subprocess.run(
        [sys.executable, "-m", "formfetch", str(cli_preview)],
        cwd=REPO, capture_output=True, text=True)
    check(proc.returncode == 0, f"CLI preview exit 0 "
                                f"(stderr: {proc.stderr.strip()})")
    preview_out = json.loads(proc.stdout)
    check(preview_out["submitted"] is False, "CLI preview did not submit")
    got_default = [(e["name"], e["value"] if e["kind"] == "text"
                    else ("file", e["filename"]))
                   for e in preview_out["preview"]["entries"]]
    masked_default = [(k, "••••••" if k == "pw" else v)
                      for k, v in EXPECTED_DEFAULT]
    check([tuple(p) for p in got_default] == masked_default,
          "CLI preview lists the ordered controls (page defaults, "
          "password masked)")
    check(len(site.requests) == count_before + 1,
          "CLI preview sent only the page GET")

    cli_run = OUT / "cli-run.json"
    cli_out = RESULTS / "cli-export.bin"
    cli_run.write_text(json.dumps({
        "page_url": site.url + "/export",
        "form_id": "export-form",
        "submitter_id": "btn-download",
        "modifications": {k: v for k, v in mods_flow1.items()},
        "output": str(cli_out),
        "boundary": BOUNDARY,
    }), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "formfetch", str(cli_run)],
        cwd=REPO, capture_output=True, text=True)
    check(proc.returncode == 0, f"CLI run exit 0 "
                                f"(stderr: {proc.stderr.strip()})")
    run_out = json.loads(proc.stdout)
    check(run_out["submitted"] is True
          and run_out["result"]["sha256"] == DATA_SHA256,
          "CLI run delivered the data file")

    cli_bad = OUT / "cli-bad.json"
    cli_bad.write_text(json.dumps({
        "page_url": site.url + "/export",
        "form_id": "no-such-form",
        "output": str(RESULTS / "never.bin"),
    }), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "formfetch", str(cli_bad)],
        cwd=REPO, capture_output=True, text=True)
    check(proc.returncode == 2
          and "FormStructureError" in proc.stderr,
          "CLI bad config exits 2 with a typed error")
    check(not (RESULTS / "never.bin").exists(), "no output on CLI error")

    # -- step 11: big upload streams ----------------------------------------
    log("STEP 11: 192 MiB upload streams (child process)")
    big = UPLOADS / "big.bin"
    big_size = 192 * 1024 * 1024
    block = bytearray()
    seed = hashlib.sha256(b"formfetch-big-block").digest()
    while len(block) < 1024 * 1024:
        seed = hashlib.sha256(seed).digest()
        block += seed
    block = bytes(block[:1024 * 1024])
    block = (block[:100] + b"\r\n\x00\r\n" + block[105:])
    with open(big, "wb") as fh:
        for _ in range(big_size // len(block)):
            fh.write(block)
    big_sha = sha256_file(big)
    check(big.stat().st_size == big_size, "big file is 192 MiB")
    proc = subprocess.run(
        [sys.executable, "-I", __file__, "--big-child",
         str(site.port), str(big), str(RESULTS / "big-result.bin")],
        cwd=REPO, capture_output=True, text=True)
    check(proc.returncode == 0,
          f"big-upload child exit 0 (stderr: {proc.stderr.strip()[:300]})")
    child_out = dict(line.split("=", 1)
                     for line in proc.stdout.strip().splitlines()
                     if "=" in line)
    peak_kb = int(child_out["CHILD_PEAK_RSS_KB"])
    check(child_out["CHILD_RESULT_SHA256"] == DATA_SHA256,
          "big upload delivered the data file")
    check(peak_kb < 96 * 1024,
          f"sender peak RSS {peak_kb} KiB << 192 MiB file size "
          "(no whole-file residency)")
    log(f"  info: big file = {big_size} bytes, sender peak RSS = "
        f"{peak_kb} KiB")
    big_record = site.requests[-1]
    check(big_record["complete"]
          and big_record["body_bytes"] == big_record["declared_length"],
          "site received the full announced length")
    big_receipt = site.receipts[-1]
    big_part = [p for p in big_receipt["parts"]
                if p.get("filename") == "big.bin"][0]
    check(big_part["sha256"] == big_sha
          and big_part["size"] == big_size,
          "received big file identical (sha256, size)")

    # -- artifacts ------------------------------------------------------------
    log("ARTIFACTS: expected parameters and upload manifest")
    (OUT / "expected_params.json").write_text(json.dumps({
        "note": "ordered successful controls; text values listed with "
                "\\n, the wire form normalizes them to CRLF",
        "flow1_multipart_post": [[k, v] for k, v in EXPECTED_FLOW1],
        "flow2_get_override": [[k, v] for k, v in EXPECTED_FLOW2],
        "flow3_post_urlencoded_override": [[k, v]
                                           for k, v in EXPECTED_FLOW3],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "uploads_manifest.json").write_text(json.dumps({
        p.name: {"bytes": p.stat().st_size, "sha256": sha256_file(p)}
        for p in (querylist, empty, tiny, big)
    }, indent=2), encoding="utf-8")
    # The 192 MiB files are reproducible evidence, not artifacts worth
    # keeping: their sizes and digests are in the manifest and the log.
    for path in [big] + [SERVER_OUT / f for f in os.listdir(SERVER_OUT)
                         if f.endswith(".bin") and
                         os.path.getsize(SERVER_OUT / f) > 64 * 1024 * 1024]:
        os.unlink(path)
        log(f"  removed reproducible big file: {path.name}")
    log(f"artifacts under {OUT}")


if __name__ == "__main__":
    main()
