"""formfetch -- form-based export fetching for the scraping toolkit.

Some public data sites have no copyable download URL: the researcher
opens an export page, picks a region and observation kinds, maybe
uploads a list of query ids, and clicks an "export" button; hidden
fields and the session cookie must ride along with that one submission.

This package turns the controls of such a page into the correct HTTP
request and brings the returned data file home.  It is *only* the
reusable form-session and request-construction piece: no URL
scheduling, no retry queue, no page archive (those live elsewhere in
the repository or are out of scope).

The one flow that runs end to end::

    from formfetch import FormSession, fetch_page, prepare_submission, submit

    session = FormSession()
    page = fetch_page(session, "http://127.0.0.1:8000/export")
    prepared = prepare_submission(
        page, "export-form",
        modifications={"region": "north", "kinds": ["opt-temp"],
                       "upload": ["/data/query-ids.txt"]},
        submitter_id="btn-download")
    print(prepared.describe())          # inspect; nothing sent yet
    result = submit(session, prepared, "/data/export.bin")

``prepare_submission`` is pure -- inspecting it never sends the submit
request.  The CLI takes the same choices as one JSON object::

    python -m formfetch config.json          # fetch + submit
    python -m formfetch --preview config.json  # stop before submitting

Standard library only: no JavaScript, no browser, no login or captcha
simulation.
"""

from .api import (Limits, FormPage, PreparedSubmission, SubmitResult,
                  fetch_page, prepare_submission, submit)
from .encode import normalize_newlines
from .errors import (ConfigError, EncodeError, FormFetchError,
                     FormStructureError, HtmlSubsetError, ModificationError,
                     NetworkError, OutputFileError, PageDecodeError,
                     ResponseStatusError, SizeLimitError,
                     UnsupportedFeatureError)
from .forms import FileEntry, TextEntry
from .session import FormSession

__all__ = [
    "FormSession",
    "fetch_page",
    "prepare_submission",
    "submit",
    "Limits",
    "FormPage",
    "PreparedSubmission",
    "SubmitResult",
    "TextEntry",
    "FileEntry",
    "normalize_newlines",
    "FormFetchError",
    "ConfigError",
    "NetworkError",
    "ResponseStatusError",
    "SizeLimitError",
    "PageDecodeError",
    "HtmlSubsetError",
    "FormStructureError",
    "ModificationError",
    "UnsupportedFeatureError",
    "EncodeError",
    "OutputFileError",
]

__version__ = "1.0.0"
