"""High-level orchestration: fetch the page, prepare, then submit.

The three steps are deliberately separate:

1. :func:`fetch_page` -- the only step that touches the network before
   the caller has seen anything; GETs the export page (keeping cookies
   in the session) and parses it;
2. :func:`prepare_submission` -- **pure**: applies modifications,
   resolves the submitter and the target, and computes the ordered
   successful-control entries.  It performs no network I/O, so the
   caller can inspect the result and decide not to send;
3. :func:`submit` -- sends exactly one request and streams the response
   body to a temporary file, moving it into place only on success.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from dataclasses import dataclass
from urllib.parse import quote_plus, urldefrag, urlsplit, urlunsplit

from .encode import build_multipart, normalize_newlines, urlencode_entries
from .errors import (OutputFileError, PageDecodeError, SizeLimitError,
                     UnsupportedFeatureError)
from .forms import (MULTIPART, URLENCODED, FileEntry, FormDocument,
                    TextEntry, apply_modifications, resolve_submitter,
                    resolve_target, successful_entries)
from .htmldoc import parse_html
from .session import FormSession

PASSWORD_MASK = "••••••"


@dataclass
class Limits:
    """Byte caps.  Defaults are generous but finite; override per call."""
    page_bytes: int = 16 * 1024 * 1024
    upload_bytes: int = 1024 * 1024 * 1024
    result_bytes: int = 256 * 1024 * 1024


class FormPage:
    """A fetched and parsed export page."""

    def __init__(self, url, headers, model):
        self.url = url
        self.headers = headers
        self.model = model          # FormDocument

    def form_ids(self):
        return [f.form_id for f in self.model.forms]


def fetch_page(session: FormSession, url: str, *,
               limits: Limits | None = None) -> FormPage:
    """GET the export page and parse it into a :class:`FormPage`."""
    limits = limits or Limits()
    url = urldefrag(url)[0]
    fetched = session.get_bytes(url, max_bytes=limits.page_bytes)
    charset = fetched.headers.get_content_charset() or "utf-8"
    try:
        text = fetched.body.decode(charset, errors="strict")
    except (UnicodeDecodeError, LookupError) as exc:
        raise PageDecodeError(
            f"GET {url}: cannot decode the page as {charset}: {exc}") \
            from exc
    model = FormDocument(parse_html(text), url)
    return FormPage(url, fetched.headers, model)


class PreparedSubmission:
    """Everything needed to send the form -- inspectable, and inert.

    Inspecting this object never sends anything; only
    :func:`submit` performs the request.
    """

    def __init__(self, *, method, url, enctype, entries, boundary,
                 form_id, submitter_id, body):
        self.method = method            # "GET" | "POST"
        self.url = url                  # final target (GET: query built)
        self.enctype = enctype
        self.entries = entries          # ordered TextEntry/FileEntry
        self.boundary = boundary
        self.form_id = form_id
        self.submitter_id = submitter_id
        self.body = body                # bytes for POST urlencoded, else
                                        # None (GET / multipart streams)

    def describe(self):
        """JSON-safe preview; password values are masked everywhere."""
        entries = []
        for entry in self.entries:
            if isinstance(entry, TextEntry):
                entries.append({
                    "kind": "text",
                    "name": entry.name,
                    "value": (PASSWORD_MASK if entry.control.is_password
                              else entry.value),
                    "control_id": entry.control.control_id,
                })
            else:
                entries.append({
                    "kind": "file",
                    "name": entry.name,
                    "filename": entry.filename,
                    "size": entry.size,
                    "selected": entry.path is not None,
                    "control_id": entry.control.control_id,
                })
        return {
            "method": self.method,
            "url": self._display_url(),
            "enctype": self.enctype,
            "form_id": self.form_id,
            "submitter_id": self.submitter_id,
            "entries": entries,
        }

    def _display_url(self):
        if self.method != "GET":
            return self.url
        if not any(isinstance(e, TextEntry) and e.control.is_password
                   for e in self.entries):
            return self.url
        # GET puts every value into the query; rebuild a masked one so
        # the preview cannot leak a password.
        parts = urlsplit(self.url)
        pairs = []
        for entry in self.entries:
            if isinstance(entry, TextEntry):
                value = (PASSWORD_MASK if entry.control.is_password
                         else normalize_newlines(entry.value))
            else:
                value = ""
            pairs.append(quote_plus(entry.name, encoding="utf-8") + "="
                         + quote_plus(value, encoding="utf-8"))
        return urlunsplit((parts.scheme, parts.netloc, parts.path,
                           "&".join(pairs), ""))


def prepare_submission(page: FormPage, form_id: str, *,
                       modifications=None, submitter_id=None,
                       limits: Limits | None = None,
                       boundary: str | None = None) -> PreparedSubmission:
    """Build the submission from page defaults plus ``modifications``.

    Pure: no network I/O.  All validation errors are raised here, before
    anything is sent.
    """
    limits = limits or Limits()
    model = page.model
    form = model.form_by_id(form_id)
    if modifications:
        apply_modifications(model, form, modifications)
    submitter = resolve_submitter(model, form, submitter_id)
    target = resolve_target(model, form, submitter)
    entries = successful_entries(model, form, submitter)

    selected = [e for e in entries
                if isinstance(e, FileEntry) and e.path is not None]
    if selected and (target.method != "POST" or target.enctype != MULTIPART):
        raise UnsupportedFeatureError(
            f"{len(selected)} file(s) selected but the submission is "
            f"{target.method} {target.enctype}; file upload requires "
            "POST multipart/form-data")
    total_upload = sum(e.size for e in selected)
    if total_upload > limits.upload_bytes:
        raise SizeLimitError(
            f"selected uploads total {total_upload} bytes, over the "
            f"limit of {limits.upload_bytes}")

    body = None
    if target.method == "GET":
        # Rebuild the query from the ordered entries, replacing the
        # action URL's original query; the path is untouched.
        query = urlencode_entries(entries).decode("ascii")
        parts = urlsplit(target.url)
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, query,
                          ""))
    else:
        url = target.url              # POST keeps the action's query
        if target.enctype == URLENCODED:
            body = urlencode_entries(entries)
    if boundary is None:
        boundary = "----formfetch-" + secrets.token_hex(16)
    return PreparedSubmission(
        method=target.method, url=url, enctype=target.enctype,
        entries=entries, boundary=boundary, form_id=form_id,
        submitter_id=submitter_id, body=body)


@dataclass
class SubmitResult:
    path: str
    bytes_written: int
    sha256: str
    status: int

    def to_dict(self):
        return {
            "path": self.path,
            "bytes_written": self.bytes_written,
            "sha256": self.sha256,
            "status": self.status,
        }


def submit(session: FormSession, prepared: PreparedSubmission,
           output_path, *, limits: Limits | None = None) -> SubmitResult:
    """Send the prepared submission and deliver the raw response bytes.

    The body streams into a temporary file next to ``output_path`` and
    is moved into place only on success.  A failure never leaves a
    complete-looking result and never overwrites an existing file.
    """
    limits = limits or Limits()
    out = os.fspath(output_path)
    if os.path.exists(out):
        raise OutputFileError(
            f"output path already exists, refusing to overwrite: {out}")
    parent = os.path.dirname(os.path.abspath(out))
    if not os.path.isdir(parent):
        raise OutputFileError(
            f"output directory does not exist: {parent}")

    if prepared.method == "POST":
        if prepared.enctype == MULTIPART:
            multipart = build_multipart(prepared.entries,
                                        prepared.boundary)
            body = multipart.iter_chunks()
            content_length = multipart.content_length
            content_type = multipart.content_type
        else:
            body = prepared.body
            content_length = len(body)
            content_type = URLENCODED
    else:
        body = None
        content_length = None
        content_type = None

    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".formfetch-",
                               suffix=".part")
    try:
        with os.fdopen(fd, "wb") as sink:
            sent = session.send_to_file(
                method=prepared.method, url=prepared.url,
                content_type=content_type, body=body,
                content_length=content_length, sink=sink,
                max_bytes=limits.result_bytes)
        try:
            # Atomic, and fails rather than overwriting an existing file.
            os.link(tmp, out)
        except FileExistsError:
            raise OutputFileError(
                f"output path appeared while downloading: {out}")
        os.unlink(tmp)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return SubmitResult(path=out, bytes_written=sent.bytes_written,
                        sha256=sent.sha256, status=sent.status)
