"""The local download mapping: source URL -> already-downloaded file.

Mapping file format (JSON, UTF-8, no duplicate JSON keys)::

    {"files": [
        {"source_url": "https://example.com/sitemap.xml", "path": "files/a.xml"},
        {"source_url": "https://example.com/sitemap-2.xml", "path": "files/b.xml.gz"}
    ]}

``path`` is resolved relative to the directory containing the mapping
file (absolute paths are taken as-is).  Two entries whose ``source_url``
normalize to the same identity (scheme/host/default-port normalization;
path and query byte-exact) are a configuration conflict: the file is
rejected as a whole and neither entry is used.

Files are only read lazily, at the moment their map is reached.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Union

from .errors import ConfigurationError, MappingConflictError, URLRuleError
from .urls import ParsedURL, parse_url

PathLike = Union[str, "Path"]


@dataclass(frozen=True)
class MappingEntry:
    source_url: str      # raw value from the mapping file
    parsed: ParsedURL    # validated canonical form
    path: str            # absolute local path
    index: int = 0       # 0-based position in the mapping document

    @property
    def identity(self) -> str:
        return self.parsed.identity


class DownloadMapping:
    """Validated, in-memory (index only) source URL -> local path mapping."""

    def __init__(self, entries: dict[str, MappingEntry],
                 mapping_path: Optional[str] = None,
                 raw_count: int = 0):
        self._entries = entries
        self.mapping_path = mapping_path
        self.raw_count = raw_count

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def get(self, identity: str) -> Optional[MappingEntry]:
        return self._entries.get(identity)

    def iter_entries(self) -> Iterator[MappingEntry]:
        return iter(self._entries.values())

    # ------------------------------------------------------------------
    @classmethod
    def from_file(cls, mapping_path: PathLike) -> "DownloadMapping":
        path = os.path.abspath(os.fspath(mapping_path))
        base = os.path.dirname(path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            raise ConfigurationError(
                f"cannot read mapping file {path!r}: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise ConfigurationError(
                f"mapping file {path!r} is not UTF-8: {exc}") from exc
        try:
            document = json.loads(text, object_pairs_hook=_no_duplicate_keys)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(
                f"mapping file {path!r} is not valid JSON: "
                f"line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
        if not isinstance(document, dict) or "files" not in document:
            raise ConfigurationError(
                f"mapping file {path!r} must be a JSON object with a "
                f"'files' array")
        extra = set(document) - {"files"}
        if extra:
            raise ConfigurationError(
                f"mapping file {path!r} has unknown top-level key(s): "
                f"{sorted(extra)}")
        raw_entries = document["files"]
        if not isinstance(raw_entries, list):
            raise ConfigurationError(
                f"mapping file {path!r}: 'files' must be an array")
        entries: dict[str, MappingEntry] = {}
        for index, item in enumerate(raw_entries):
            where = f"mapping entry #{index}"
            if not isinstance(item, dict):
                raise ConfigurationError(f"{where} is not a JSON object")
            keys = set(item)
            if keys != {"source_url", "path"}:
                missing = {"source_url", "path"} - keys
                unknown = keys - {"source_url", "path"}
                bits = []
                if missing:
                    bits.append(f"missing {sorted(missing)}")
                if unknown:
                    bits.append(f"unknown field(s) {sorted(unknown)}")
                raise ConfigurationError(f"{where}: " + ", ".join(bits))
            source_url = item["source_url"]
            rel_path = item["path"]
            if not isinstance(source_url, str) or not source_url:
                raise ConfigurationError(
                    f"{where}: source_url must be a non-empty string")
            if not isinstance(rel_path, str) or not rel_path:
                raise ConfigurationError(
                    f"{where}: path must be a non-empty string")
            try:
                parsed = parse_url(source_url)
            except URLRuleError as exc:
                raise ConfigurationError(
                    f"{where}: source_url rejected: {exc}") from exc
            entry = MappingEntry(
                source_url=source_url, parsed=parsed,
                path=os.path.normpath(os.path.join(base, rel_path)),
                index=index)
            previous = entries.get(entry.identity)
            if previous is not None:
                raise MappingConflictError(
                    f"mapping file {path!r}: duplicate source_url "
                    f"(normalized {entry.identity!r}) at entries "
                    f"#{previous.index} and #{index}",
                    first_index=previous.index,
                    first_url=previous.source_url,
                    second_index=index, second_url=source_url)
            entries[entry.identity] = entry
        return cls(entries, mapping_path=path, raw_count=len(raw_entries))

    # ------------------------------------------------------------------
    @classmethod
    def from_pairs(cls, pairs, *, base_dir: Optional[PathLike] = None,
                   ) -> "DownloadMapping":
        """Build a mapping from ``(source_url, path)`` pairs (API helper).

        Used by tests and demos; duplicate identities raise
        :class:`MappingConflictError` just like the file form.
        """
        base = os.path.abspath(os.fspath(base_dir)) if base_dir else os.getcwd()
        entries: dict[str, MappingEntry] = {}
        count = 0
        for index, (source_url, rel_path) in enumerate(pairs):
            count += 1
            parsed = parse_url(source_url)
            entry = MappingEntry(
                source_url=source_url, parsed=parsed,
                path=os.path.normpath(os.path.join(base, rel_path)),
                index=index)
            previous = entries.get(entry.identity)
            if previous is not None:
                raise MappingConflictError(
                    f"duplicate source_url (normalized {entry.identity!r})",
                    first_index=previous.index, first_url=previous.source_url,
                    second_index=index, second_url=source_url)
            entries[entry.identity] = entry
        return cls(entries, raw_count=count)


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError(
                f"mapping file contains duplicate JSON key {key!r}")
        result[key] = value
    return result
