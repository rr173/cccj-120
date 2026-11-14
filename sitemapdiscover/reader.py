"""Streaming reader for one sitemap file.

The file is read in fixed-size chunks and never loaded whole:

1. gzip is detected by the **magic bytes** ``1f 8b`` (never by extension);
2. gzip payload is inflated incrementally with an output cap, so a
   compression bomb stops at ``max_decompressed_bytes`` instead of
   exhausting memory;
3. bytes are decoded to text with a strict incremental UTF-8 decoder and
   fed to expat piece by piece;
4. entries are validated and handed to an ``on_entry`` sink one at a
   time (the engine stages them in SQLite).

Every problem raises :class:`MapRejection` with a stable kind and a
concrete reason.  The caller must treat the map atomically: the sink may
already hold entries, but they only become visible after this function
returns normally, i.e. after gzip CRC/length, the XML tail and every
entry have been validated.

Supported content:
    UTF-8 XML with root ``urlset`` or ``sitemapindex`` in
    ``http://www.sitemaps.org/schemas/sitemap/0.9``.

Entry rules (both roots):
    * one direct ``loc``; its trimmed text must be a valid absolute
      http(s) URL of the map's own origin;
    * at most one direct ``lastmod``, strictly ``YYYY-MM-DD``;
    * unknown fields in the sitemap namespace are skipped;
    * any element in another namespace is ignored **with its whole
      subtree**, so an extension ``loc`` can never masquerade as an
      official one;
    * DTDs and entity declarations are rejected (no entity fetching, no
      entity expansion).
"""

from __future__ import annotations

import codecs
import datetime
import re
import zlib
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple
from xml.parsers import expat

from .errors import URLRuleError, MapRejection
from .profile import Limits
from .urls import ParsedURL, parse_url

SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
NS_SEP = "\x1f"

_READ_CHUNK = 64 * 1024
_INFLATE_CHUNK = 256 * 1024   # max decompressed bytes produced per call
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_ASCII_WS = " \t\r\n"
_WS_SET = frozenset(_ASCII_WS)
_HEADER_CAP = 256 * 1024


@dataclass(frozen=True)
class EntryRecord:
    """One validated url/sitemap entry, in document order."""

    index: int                 # 1-based entry number inside this map
    kind: str                  # "page" | "sitemap"
    loc: str                   # trimmed original loc text
    parsed: ParsedURL          # validated canonical URL
    lastmod: Optional[str]     # validated YYYY-MM-DD, or None
    line: int                  # source line of the entry's start tag


@dataclass(frozen=True)
class MapSummary:
    """What one successfully validated map contained."""

    root_kind: str             # "urlset" | "sitemapindex"
    entries: int               # number of url/sitemap entries
    pages: int
    sitemaps: int
    root_line: int


def _reject(kind: str, reason: str, *, entry_index=None, line=None,
            column=None) -> MapRejection:
    return MapRejection(kind, reason, entry_index=entry_index,
                        line=line, column=column)


# ----------------------------------------------------------------------
# bounded text accumulator for <loc>/<lastmod>
# ----------------------------------------------------------------------
class _TrimmedText:
    """Accumulate element text with XML end-trim semantics and a cap.

    Leading and trailing XML whitespace is dropped.  Interior whitespace
    is never silently removed: for both ``loc`` and ``lastmod`` any
    interior whitespace makes the value invalid, and the rejection is
    raised when the second non-whitespace run appears.  A huge
    whitespace-only tail is *counted*, not stored.
    """

    __slots__ = ("parts", "started", "pending", "length", "limit",
                 "what", "entry_index")

    def __init__(self, limit: int, what: str, entry_index: Optional[int]):
        self.parts: List[str] = []
        self.started = False
        self.pending = 0          # trailing whitespace run, not stored
        self.length = 0           # stored (trimmed-so-far) character count
        self.limit = limit
        self.what = what
        self.entry_index = entry_index

    def _interior_ws(self) -> MapRejection:
        if self.what == "loc":
            return _reject(
                "invalid_loc",
                f"entry #{self.entry_index} <loc> contains interior ASCII "
                f"whitespace, which is not allowed in a URL",
                entry_index=self.entry_index)
        return _reject(
            "invalid_lastmod",
            f"entry #{self.entry_index} <lastmod> contains interior ASCII "
            f"whitespace",
            entry_index=self.entry_index)

    def add(self, text: str) -> None:
        if not text:
            return
        pos = 0
        if not self.started:
            while pos < len(text) and text[pos] in _WS_SET:
                pos += 1
            if pos == len(text):
                return  # leading whitespace only; not stored
            self.started = True
        if pos == 0 and all(ch in _WS_SET for ch in text):
            # A pure whitespace run after content: count it, never store.
            self.pending += len(text)
            return
        if self.pending:
            # Whitespace was seen and now real content follows it.
            raise self._interior_ws()
        body = text[pos:]
        end = len(body)
        while end > 0 and body[end - 1] in _WS_SET:
            end -= 1
        if end == 0:
            self.pending += len(body)
            return
        piece = body[:end]
        self.pending = len(body) - end
        self.length += len(piece)
        if self.length > self.limit:
            if self.what == "loc":
                raise _reject(
                    "limit_loc_length",
                    f"entry #{self.entry_index} <loc> is longer than "
                    f"max_loc_length={self.limit}",
                    entry_index=self.entry_index)
            raise _reject(
                "invalid_lastmod",
                f"entry #{self.entry_index} <lastmod> is longer than "
                f"{self.limit} characters",
                entry_index=self.entry_index)
        self.parts.append(piece)

    def finalize(self) -> str:
        return "".join(self.parts)


# ----------------------------------------------------------------------
# byte streams: raw file cap, gzip magic detection, bounded inflation
# ----------------------------------------------------------------------
def _open_file(path: str):
    try:
        return open(path, "rb")
    except FileNotFoundError as exc:
        raise _reject("missing_file",
                      f"local file {path!r} does not exist") from exc
    except OSError as exc:
        raise _reject("unreadable", f"cannot open {path!r}: {exc}") from exc


def _raw_chunks(fh, limits: Limits, path: str):
    """Yield raw file bytes; enforce the compressed-byte cap."""
    total = 0
    while True:
        try:
            chunk = fh.read(_READ_CHUNK)
        except OSError as exc:
            raise _reject("unreadable",
                          f"error reading {path!r}: {exc}") from exc
        if not chunk:
            return
        total += len(chunk)
        if total > limits.max_file_bytes:
            raise _reject(
                "limit_file_bytes",
                f"compressed file {path!r} exceeds max_file_bytes="
                f"{limits.max_file_bytes} (read {total} bytes)")
        yield chunk


def _gzip_chunks(fh, limits: Limits, path: str, head: bytes = b""):
    """Yield decompressed bytes of exactly one gzip member."""
    d = zlib.decompressobj(wbits=31)
    produced = 0
    pending = b""
    got_eof = False
    try:
        for chunk in _iter_with_head(fh, limits, path, head):
            data = pending + chunk
            pending = b""
            while data and not got_eof:
                remaining = limits.max_decompressed_bytes - produced
                # Bound each call as well, so a very high compression
                # ratio cannot allocate a huge output buffer at once.
                out = d.decompress(
                    data, min(remaining + 1, _INFLATE_CHUNK))
                produced += len(out)
                if produced > limits.max_decompressed_bytes:
                    raise _reject(
                        "limit_decompressed",
                        f"decompressed size of {path!r} exceeds "
                        f"max_decompressed_bytes="
                        f"{limits.max_decompressed_bytes}")
                if out:
                    yield out
                if d.eof:
                    got_eof = True
                    pending = d.unused_data
                    break
                data = d.unconsumed_tail
            if got_eof:
                break
    except zlib.error as exc:
        msg = str(exc)
        if "incorrect data check" in msg or "incorrect length check" in msg:
            raise _reject("gzip_crc",
                          f"gzip CRC/length check failed for {path!r}: "
                          f"{msg}") from exc
        if "incomplete" in msg or "truncated" in msg:
            raise _reject("gzip_truncated",
                          f"gzip stream is truncated: {msg}") from exc
        raise _reject("gzip_invalid",
                      f"invalid gzip data in {path!r}: {msg}") from exc

    if not got_eof:
        raise _reject(
            "gzip_truncated",
            f"gzip member in {path!r} ends without a complete trailer")
    trail = pending
    while len(trail) < 2:
        more = fh.read(_READ_CHUNK)
        if not more:
            break
        trail += more
    if trail:
        if trail[:2] == b"\x1f\x8b":
            raise _reject(
                "gzip_concatenated",
                f"{path!r} contains a second gzip member; exactly one "
                f"member is accepted")
        raise _reject(
            "gzip_trailing",
            f"{path!r} has {len(trail)} byte(s) of trailing data after "
            f"the gzip member")


def _iter_with_head(fh, limits: Limits, path: str, head: bytes):
    first = True
    total = 0
    for chunk in _raw_chunks(fh, limits, path):
        if first and head:
            chunk = head + chunk
            first = False
        yield chunk
    if first and head:
        yield head


def _plain_chunks(fh, limits: Limits, path: str, head: bytes = b""):
    """Yield bytes of an uncompressed file, enforcing both byte caps."""
    total = len(head)
    yield from (() if not head else (head,))
    for chunk in _raw_chunks(fh, limits, path):
        total += len(chunk)
        if total > limits.max_decompressed_bytes:
            raise _reject(
                "limit_decompressed",
                f"file {path!r} exceeds max_decompressed_bytes="
                f"{limits.max_decompressed_bytes}")
        yield chunk


def content_chunks(path: str, limits: Limits):
    """Yield the (possibly decompressed) content stream of one map."""
    with _open_file(path) as fh:
        head = fh.read(2)
        if head == b"\x1f\x8b":
            yield from _gzip_chunks(fh, limits, path, head)
        else:
            yield from _plain_chunks(fh, limits, path, head)


# ----------------------------------------------------------------------
# UTF-8 + XML-declaration enforcement
# ----------------------------------------------------------------------
class _Utf8XmlText:
    """Strict incremental UTF-8 decode, BOM handling, encoding check."""

    __slots__ = ("decoder", "offset", "header", "header_done", "saw_bom")

    def __init__(self):
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.offset = 0
        self.header = ""
        self.header_done = False
        self.saw_bom = False

    def feed(self, data: bytes) -> str:
        try:
            text = self.decoder.decode(data, False)
        except UnicodeDecodeError as exc:
            raise _reject(
                "invalid_utf8",
                f"map content is not valid UTF-8 near decompressed offset "
                f"{self.offset + exc.start}: {exc.reason}") from exc
        self.offset += len(data)
        return self._gate(text)

    def close(self) -> str:
        try:
            text = self.decoder.decode(b"", True)
        except UnicodeDecodeError as exc:
            raise _reject(
                "invalid_utf8",
                f"map content ends inside a UTF-8 sequence near offset "
                f"{self.offset}: {exc.reason}") from exc
        return self._gate(text)

    def _gate(self, text: str) -> str:
        if self.header_done:
            return text
        self.header += text
        prefix = self.header
        if not self.saw_bom and prefix.startswith("﻿"):
            self.saw_bom = True
            prefix = prefix[1:]
        stripped = prefix.lstrip(_ASCII_WS)
        if not stripped:
            if len(self.header) > _HEADER_CAP:
                raise _reject(
                    "invalid_xml",
                    "map starts with more than 256 KiB of whitespace and "
                    "no XML element")
            return ""
        if not stripped.startswith("<?xml"):
            # A short first chunk may be only a prefix of the XML
            # declaration; keep buffering until it is disproved.
            if len(stripped) < 5 and "<?xml".startswith(stripped):
                return ""
            return self._flush()
        end = stripped.find("?>")
        if end < 0:
            if len(self.header) > _HEADER_CAP:
                raise _reject(
                    "invalid_xml_declaration",
                    "XML declaration is not terminated by '?>'")
            return ""
        decl = stripped[:end + 2]
        m = re.search(r"""encoding\s*=\s*(["'])([^"']*)\1""", decl)
        if m is not None:
            declared = m.group(2)
            if declared.lower().replace("_", "-") not in ("utf-8", "utf8"):
                raise _reject(
                    "unsupported_encoding",
                    f"XML declares encoding={declared!r}; only UTF-8 "
                    f"content is accepted")
        return self._flush()

    def _flush(self) -> str:
        self.header_done = True
        out = self.header
        self.header = ""
        return out


# ----------------------------------------------------------------------
# XML structure validator
# ----------------------------------------------------------------------
class _MapParser:
    """Expat handlers implementing the sitemap subset.

    Depth convention: the root element is depth 1, each url/sitemap
    entry is depth 2, and ``loc``/``lastmod`` are depth 3.
    """

    def __init__(self, map_origin: str, limits: Limits,
                 on_entry: Callable[[EntryRecord], None]):
        self.map_origin = map_origin
        self.limits = limits
        self.on_entry = on_entry
        parser = expat.ParserCreate(namespace_separator=NS_SEP)
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        parser.StartElementHandler = self._start
        parser.EndElementHandler = self._end
        parser.CharacterDataHandler = self._chars
        parser.StartDoctypeDeclHandler = self._doctype
        parser.EntityDeclHandler = self._entity
        parser.UnparsedEntityDeclHandler = self._unparsed_entity
        parser.ExternalEntityRefHandler = self._external_entity
        parser.CommentHandler = lambda data: None
        parser.ProcessingInstructionHandler = lambda target, data: None
        parser.StartCdataSectionHandler = lambda: None
        parser.EndCdataSectionHandler = lambda: None
        self.parser = parser

        self.depth = 0
        self.root_kind: Optional[str] = None
        self.root_line = 0
        self.root_closed = False
        self.skip_until_depth: Optional[int] = None

        self.in_entry = False
        self.entry_kind = ""
        self.entry_index = 0
        self.entry_line = 0
        self.loc_count = 0
        self.lastmod_count = 0
        self.loc_parsed: Optional[ParsedURL] = None
        self.lastmod: Optional[str] = None
        self.loc_buffer: Optional[_TrimmedText] = None
        self.lastmod_buffer: Optional[_TrimmedText] = None
        self.loc_open = False
        self.lastmod_open = False

        self.pages = 0
        self.sitemaps = 0
        self.entries = 0

    # -- helpers -------------------------------------------------------
    def _where(self) -> Tuple[int, int]:
        return (self.parser.CurrentLineNumber,
                self.parser.CurrentColumnNumber)

    def _reject(self, kind: str, reason: str) -> MapRejection:
        line, col = self._where()
        return _reject(kind, reason, entry_index=self.entry_index
                       if self.in_entry else None, line=line, column=col)

    @staticmethod
    def _split(name: str) -> Tuple[Optional[str], str]:
        if NS_SEP in name:
            ns, local = name.split(NS_SEP, 1)
            return ns, local
        return None, name

    def _start_skip(self) -> None:
        # The element has already been counted; ignore it and everything
        # below it until its own end tag.
        self.skip_until_depth = self.depth

    # -- expat handlers ------------------------------------------------
    def _doctype(self, *args):
        raise self._reject(
            "doctype_forbidden",
            "DTD/DOCTYPE declarations are not accepted in a sitemap")

    def _entity(self, *args):
        raise self._reject(
            "entity_forbidden",
            "entity declarations are not accepted in a sitemap")

    def _unparsed_entity(self, *args):
        raise self._reject(
            "entity_forbidden",
            "unparsed entity declarations are not accepted in a sitemap")

    def _external_entity(self, *args):
        raise self._reject(
            "entity_forbidden",
            "external entity references are not accepted in a sitemap")

    def _start(self, name, attrs):
        self.depth += 1
        if self.skip_until_depth is not None:
            return
        ns, local = self._split(name)

        if self.depth == 1:
            if ns != SITEMAP_NS or local not in ("urlset", "sitemapindex"):
                raise self._reject(
                    "bad_root",
                    f"root element {local!r} in namespace {ns!r} is not "
                    f"urlset/sitemapindex in {SITEMAP_NS!r}")
            self.root_kind = local
            self.root_line = self.parser.CurrentLineNumber
            return

        if self.root_kind is None or self.root_closed:
            raise self._reject("bad_root", "content outside the root element")

        if self.depth == 2:
            if ns != SITEMAP_NS:
                self._start_skip()
                return
            expected = "url" if self.root_kind == "urlset" else "sitemap"
            other = "sitemap" if expected == "url" else "url"
            if local == other:
                # A urlset carrying <sitemap> (or the reverse) is an
                # authoring error that would silently lose entries if
                # skipped, so the map is rejected instead.
                raise self._reject(
                    "mismatched_entry",
                    f"<{local}> is not allowed inside <{self.root_kind}> "
                    f"(expected <{expected}> entries)")
            if local != expected:
                self._start_skip()
                return
            self.entry_index += 1
            if self.entry_index > self.limits.max_entries_per_map:
                raise _reject(
                    "limit_entries",
                    f"map has more than max_entries_per_map="
                    f"{self.limits.max_entries_per_map} entries "
                    f"(stopped at entry #{self.entry_index})",
                    entry_index=self.entry_index,
                    line=self.parser.CurrentLineNumber)
            self.in_entry = True
            self.entry_kind = "page" if expected == "url" else "sitemap"
            self.entry_line = self.parser.CurrentLineNumber
            self.loc_count = 0
            self.lastmod_count = 0
            self.loc_parsed = None
            self.lastmod = None
            self.loc_buffer = None
            self.lastmod_buffer = None
            self.loc_open = False
            self.lastmod_open = False
            return

        if self.depth == 3 and self.in_entry:
            if ns != SITEMAP_NS:
                self._start_skip()
                return
            if local == "loc":
                self.loc_count += 1
                if self.loc_count > 1:
                    raise self._reject(
                        "duplicate_loc",
                        f"entry #{self.entry_index} has more than one "
                        f"direct <loc>")
                self.loc_open = True
                self.loc_buffer = _TrimmedText(
                    self.limits.max_loc_length, "loc", self.entry_index)
                return
            if local == "lastmod":
                self.lastmod_count += 1
                if self.lastmod_count > 1:
                    raise self._reject(
                        "duplicate_lastmod",
                        f"entry #{self.entry_index} has more than one "
                        f"direct <lastmod>")
                self.lastmod_open = True
                self.lastmod_buffer = _TrimmedText(64, "lastmod",
                                                   self.entry_index)
                return
            self._start_skip()
            return

        if self.depth == 4 and (self.loc_open or self.lastmod_open):
            raise self._reject(
                "loc_has_children" if self.loc_open else "invalid_lastmod",
                f"<{'loc' if self.loc_open else 'lastmod'}> in entry "
                f"#{self.entry_index} must contain text only")

        # Defensive: any other nested element is outside the subset.
        self._start_skip()

    def _end(self, name):
        depth = self.depth
        if self.skip_until_depth is not None:
            if depth == self.skip_until_depth:
                self.skip_until_depth = None
            self.depth -= 1
            return
        ns, local = self._split(name)

        if depth == 3 and self.in_entry:
            if self.loc_open:
                self._finish_loc()
                self.loc_open = False
                self.loc_buffer = None
            elif self.lastmod_open:
                self._finish_lastmod()
                self.lastmod_open = False
                self.lastmod_buffer = None
            self.depth -= 1
            return

        if depth == 2 and self.in_entry:
            self._finish_entry()
            self.depth -= 1
            return

        if depth == 1:
            self.root_closed = True
        self.depth -= 1

    def _chars(self, text):
        if self.skip_until_depth is not None or not text:
            return
        if self.loc_open:
            if self.depth != 3:
                raise self._reject("loc_has_children",
                                   "unexpected nested content in <loc>")
            self.loc_buffer.add(text)
            return
        if self.lastmod_open:
            if self.depth != 3:
                raise self._reject("invalid_lastmod",
                                   "unexpected nested content in <lastmod>")
            self.lastmod_buffer.add(text)
            return
        if text.strip(_ASCII_WS):
            raise self._reject(
                "unexpected_text",
                "unexpected character data in the sitemap structure")

    # -- entry finalization -------------------------------------------
    def _finish_loc(self):
        text = self.loc_buffer.finalize()
        if not text:
            raise self._reject(
                "empty_loc",
                f"entry #{self.entry_index} has an empty <loc>")
        try:
            parsed = parse_url(text)
        except URLRuleError as exc:
            raise self._reject(
                "invalid_loc",
                f"entry #{self.entry_index} <loc> {text!r} is not an "
                f"acceptable absolute URL: {exc}") from exc
        if parsed.origin != self.map_origin:
            raise self._reject(
                "cross_origin",
                f"entry #{self.entry_index} <loc> {text!r} has origin "
                f"{parsed.origin!r} but this map belongs to "
                f"{self.map_origin!r}; cross-origin references are "
                f"rejected")
        self.loc_parsed = parsed

    def _finish_lastmod(self):
        text = self.lastmod_buffer.finalize()
        if not text:
            raise self._reject(
                "invalid_lastmod",
                f"entry #{self.entry_index} has an empty <lastmod>")
        if not _DATE_RE.fullmatch(text):
            raise self._reject(
                "invalid_lastmod",
                f"entry #{self.entry_index} <lastmod> {text!r} is not a "
                f"YYYY-MM-DD date")
        try:
            datetime.date.fromisoformat(text)
        except ValueError as exc:
            raise self._reject(
                "invalid_lastmod",
                f"entry #{self.entry_index} <lastmod> {text!r} is not a "
                f"valid calendar date: {exc}") from exc
        self.lastmod = text

    def _finish_entry(self):
        if self.loc_count == 0:
            raise self._reject(
                "missing_loc",
                f"entry #{self.entry_index} ({self.entry_kind}) has no "
                f"direct <loc>")
        if self.loc_open or self.lastmod_open:  # defensively unreachable
            raise self._reject("invalid_xml",
                               "entry ended with an open child element")
        record = EntryRecord(
            index=self.entry_index,
            kind=self.entry_kind,
            loc=self.loc_parsed.original,
            parsed=self.loc_parsed,
            lastmod=self.lastmod,
            line=self.entry_line,
        )
        self.on_entry(record)
        self.entries += 1
        if self.entry_kind == "page":
            self.pages += 1
        else:
            self.sitemaps += 1
        self.in_entry = False
        self.entry_kind = ""
        self.loc_parsed = None
        self.lastmod = None


def parse_map_file(path: str, map_origin: str, limits: Limits,
                   on_entry: Callable[[EntryRecord], None]) -> MapSummary:
    """Stream-validate one local sitemap and emit its entries.

    Returns only after the gzip trailer (or the plain file end), the XML
    tail and every entry all validated.  On any rejection the exception
    propagates and the caller must discard entries already passed to
    ``on_entry`` (they were never made visible outside the staging sink).
    """
    handler = _MapParser(map_origin, limits, on_entry)
    text_gate = _Utf8XmlText()
    parser = handler.parser
    try:
        for chunk in content_chunks(path, limits):
            decoded = text_gate.feed(chunk)
            if decoded:
                parser.Parse(decoded, False)
        tail = text_gate.close()
        if tail:
            parser.Parse(tail, False)
        parser.Parse("", True)
    except MapRejection:
        raise
    except expat.ExpatError as exc:
        raise _reject(
            "invalid_xml",
            f"XML error: {expat.ErrorString(exc.code)} "
            f"(line {exc.lineno}, column {exc.offset})",
            line=exc.lineno, column=exc.offset) from exc
    except ValueError as exc:
        raise _reject("invalid_xml",
                      f"XML parser rejected the input: {exc}") from exc

    if handler.root_kind is None:
        raise _reject("invalid_xml", "document has no root element")
    if not handler.root_closed:
        raise _reject("invalid_xml",
                      "document ended inside the root element")
    return MapSummary(root_kind=handler.root_kind,
                      entries=handler.entries,
                      pages=handler.pages,
                      sitemaps=handler.sitemaps,
                      root_line=handler.root_line)
