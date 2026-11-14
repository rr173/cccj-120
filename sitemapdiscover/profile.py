"""Tunable limits for one discovery run.

Every limit is enforced at a located point and produces a failure node
(or, for configuration errors, a configuration rejection) rather than
a silent truncation:

    max_file_bytes        compressed bytes read from one map file
                          (checked while reading, before inflating)
    max_decompressed_bytes decompressed bytes produced from one map file
                          (a gzip bomb stops at this boundary)
    max_entries_per_map    url/sitemap entries in one map (the entry
                          *after* the limit is not accepted; the map is
                          rejected as a whole)
    max_maps               distinct map files actually read in this run
                          (a shared subgraph counts once)
    max_depth              reference depth; roots are depth 0
    max_loc_length         characters in one trimmed <loc> value

All values must be integers; the byte/entry/map/depth limits must be
positive (``max_depth`` may be 0, meaning roots only may be read) and
``max_loc_length`` must be at least 1.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from .errors import LimitsError


DEFAULT_MAX_FILE_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_DECOMPRESSED_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_ENTRIES_PER_MAP = 100_000
DEFAULT_MAX_MAPS = 1_000
DEFAULT_MAX_DEPTH = 10
DEFAULT_MAX_LOC_LENGTH = 8_192


@dataclass(frozen=True)
class Limits:
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_decompressed_bytes: int = DEFAULT_MAX_DECOMPRESSED_BYTES
    max_entries_per_map: int = DEFAULT_MAX_ENTRIES_PER_MAP
    max_maps: int = DEFAULT_MAX_MAPS
    max_depth: int = DEFAULT_MAX_DEPTH
    max_loc_length: int = DEFAULT_MAX_LOC_LENGTH

    def validated(self) -> "Limits":
        for name in ("max_file_bytes", "max_decompressed_bytes",
                     "max_entries_per_map", "max_maps",
                     "max_loc_length"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise LimitsError(f"{name} must be an integer, got {value!r}")
            if value < 1:
                raise LimitsError(f"{name} must be >= 1, got {value}")
        value = self.max_depth
        if isinstance(value, bool) or not isinstance(value, int):
            raise LimitsError(f"max_depth must be an integer, got {value!r}")
        if value < 0:
            raise LimitsError(f"max_depth must be >= 0, got {value}")
        return self

    def to_dict(self) -> dict[str, int]:
        return {
            "max_file_bytes": self.max_file_bytes,
            "max_decompressed_bytes": self.max_decompressed_bytes,
            "max_entries_per_map": self.max_entries_per_map,
            "max_maps": self.max_maps,
            "max_depth": self.max_depth,
            "max_loc_length": self.max_loc_length,
        }

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "Limits":
        unknown = set(values) - set(cls.__dataclass_fields__)
        if unknown:
            raise LimitsError(f"unknown limit name(s): {sorted(unknown)}")
        limits = replace(cls(), **values)
        return limits.validated()
