"""Exception hierarchy for formfetch.

Every failure raised by this package is a :class:`FormFetchError`.  The
subclasses tell the caller *which stage* rejected the work:

* config / page fetch / HTML subset / form structure / modification /
  unsupported feature -- all raised **before** any submit request is
  sent, so a rejected submission never touches the network;
* :class:`NetworkError` / :class:`ResponseStatusError` /
  :class:`SizeLimitError` -- raised by the page GET or by the submit
  exchange itself;
* :class:`OutputFileError` -- the result path cannot be used (raised
  before the submit request is sent whenever the problem is known up
  front).
"""

from __future__ import annotations


class FormFetchError(Exception):
    """Base class for every formfetch error."""


class ConfigError(FormFetchError, ValueError):
    """The CLI/JSON configuration is invalid (rejected before network)."""


class NetworkError(FormFetchError):
    """A transport-level failure during the page GET or the submit."""


class ResponseStatusError(FormFetchError):
    """The page GET or the submit returned a non-2xx status.

    Redirects (3xx) land here too: formfetch never follows them
    automatically.  The already-received error body is kept only as a
    short diagnostic snippet, never as a deliverable result.
    """

    def __init__(self, message, *, status=None, url=None, location=None,
                 snippet=None):
        super().__init__(message)
        self.status = status
        self.url = url
        self.location = location
        self.snippet = snippet


class SizeLimitError(FormFetchError):
    """A page, upload or result byte limit was exceeded."""


class PageDecodeError(FormFetchError):
    """The fetched page bytes cannot be decoded with their charset."""


class HtmlSubsetError(FormFetchError):
    """The page uses HTML outside the supported strict subset."""


class FormStructureError(FormFetchError):
    """Form/control lookup failed: unknown id, duplicate id, wrong
    ownership, or the element is not the expected kind of control."""


class ModificationError(FormFetchError, ValueError):
    """A modification entry is invalid for its control (type, disabled
    state, ownership, unknown option, radio-group conflict, ...)."""


class UnsupportedFeatureError(FormFetchError):
    """The page or config asks for something deliberately not supported
    (image buttons, text/plain, dialog method, file upload over GET or
    urlencoded, ...)."""


class EncodeError(FormFetchError):
    """A request body cannot be encoded as specified (CR/LF in a header
    parameter, boundary problems, a file that changed size, ...)."""


class OutputFileError(FormFetchError):
    """The result path is unusable (already exists, missing directory)."""
