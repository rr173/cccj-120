"""Batch configuration and manifest validation.

A batch is described by two inputs, both validated **before any network
activity**:

* a UTF-8 JSONL manifest with one object per line, each holding a unique
  ``job_id`` and an absolute ``url`` (the manifest order is the reception
  order and is preserved everywhere downstream);
* a :class:`BatchConfig` with the global in-flight cap, per-origin budgets
  (in-flight cap and minimum gap between two real request starts of the
  same origin), one-request timeout, response body byte cap and the
  maximum number of redirects.

Origins reuse the repository URL rule (:mod:`sitemapdiscover.urls`): the
scheme/host/port normalization is identical, but http and https and every
distinct port are separate origins with separate budgets.  Path and query
are never rewritten for queuing -- the validated canonical string is the
exact address requested on the wire (only scheme/host/default-port
spelling is canonicalized, exactly as the shared URL rule defines).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from sitemapdiscover.urls import ParsedURL, parse_url

from .errors import BatchConfigError, ManifestError


@dataclass(frozen=True)
class OriginBudget:
    """Per-origin budget.

    ``inflight_cap`` concurrent exchanges at most for this origin, and
    two request starts addressed to it must be at least
    ``min_interval_seconds`` apart on the monotonic clock.
    """

    inflight_cap: int
    min_interval_seconds: float

    def validated(self, origin: str) -> "OriginBudget":
        if isinstance(self.inflight_cap, bool) or not isinstance(
                self.inflight_cap, int):
            raise BatchConfigError(
                f"inflight_cap for {origin!r} must be an integer, got "
                f"{self.inflight_cap!r}")
        if self.inflight_cap < 1:
            raise BatchConfigError(
                f"inflight_cap for {origin!r} must be >= 1, got "
                f"{self.inflight_cap}")
        if isinstance(self.min_interval_seconds, bool) or not isinstance(
                self.min_interval_seconds, (int, float)):
            raise BatchConfigError(
                f"min_interval_seconds for {origin!r} must be a number, got "
                f"{self.min_interval_seconds!r}")
        if self.min_interval_seconds < 0:
            raise BatchConfigError(
                f"min_interval_seconds for {origin!r} must be >= 0, got "
                f"{self.min_interval_seconds}")
        return self

    def to_dict(self) -> dict:
        return {
            "inflight_cap": self.inflight_cap,
            "min_interval_seconds": self.min_interval_seconds,
        }


@dataclass(frozen=True)
class BatchConfig:
    """Validated configuration of one batch."""

    global_inflight: int
    default_origin: OriginBudget
    origin_overrides: Mapping[str, OriginBudget] = field(default_factory=dict)
    request_timeout_seconds: float = 30.0
    max_body_bytes: int = 4 * 1024 * 1024
    max_redirects: int = 5

    def validated(self) -> "BatchConfig":
        if isinstance(self.global_inflight, bool) or not isinstance(
                self.global_inflight, int):
            raise BatchConfigError(
                f"global_inflight must be an integer, got "
                f"{self.global_inflight!r}")
        if self.global_inflight < 1:
            raise BatchConfigError(
                f"global_inflight must be >= 1, got {self.global_inflight}")
        if not isinstance(self.default_origin, OriginBudget):
            raise BatchConfigError(
                "default_origin must be an OriginBudget")
        self.default_origin.validated("<default>")
        if not isinstance(self.origin_overrides, Mapping):
            raise BatchConfigError("origin_overrides must be a mapping")
        overrides: dict[str, OriginBudget] = {}
        for raw_origin, budget in self.origin_overrides.items():
            if not isinstance(raw_origin, str) or not raw_origin:
                raise BatchConfigError(
                    f"origin override key must be a non-empty string, got "
                    f"{raw_origin!r}")
            if not isinstance(budget, OriginBudget):
                raise BatchConfigError(
                    f"override for {raw_origin!r} must be an OriginBudget")
            # The override key must itself be a valid canonical origin.
            try:
                canonical = parse_url(raw_origin + "/").origin
            except Exception as exc:
                raise BatchConfigError(
                    f"origin override key {raw_origin!r} is not a valid "
                    f"origin: {exc}") from exc
            budget.validated(canonical)
            overrides[canonical] = budget
        for name, value in (("request_timeout_seconds",
                             self.request_timeout_seconds),
                            ("max_body_bytes", self.max_body_bytes),
                            ("max_redirects", self.max_redirects)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise BatchConfigError(f"{name} must be a number, got {value!r}")
        if self.request_timeout_seconds <= 0:
            raise BatchConfigError(
                f"request_timeout_seconds must be > 0, got "
                f"{self.request_timeout_seconds}")
        if self.max_body_bytes < 1:
            raise BatchConfigError(
                f"max_body_bytes must be >= 1, got {self.max_body_bytes}")
        if self.max_redirects < 0:
            raise BatchConfigError(
                f"max_redirects must be >= 0, got {self.max_redirects}")
        object.__setattr__(self, "origin_overrides", overrides)
        return self

    def budget_for(self, origin: str) -> OriginBudget:
        return self.origin_overrides.get(origin, self.default_origin)

    def to_dict(self) -> dict:
        return {
            "global_inflight": self.global_inflight,
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_body_bytes": self.max_body_bytes,
            "max_redirects": self.max_redirects,
            "default_origin": self.default_origin.to_dict(),
            "origin_overrides": {k: v.to_dict()
                                  for k, v in sorted(self.origin_overrides.items())},
        }


@dataclass(frozen=True)
class JobSpec:
    """One accepted manifest line, in manifest order."""

    job_id: str
    index: int           # 0-based reception order
    raw_url: str         # exactly as written in the manifest
    parsed: ParsedURL    # validated/canonicalized URL

    @property
    def url(self) -> str:
        """The canonical address actually requested (path/query preserved)."""
        return self.parsed.identity

    @property
    def origin(self) -> str:
        return self.parsed.origin


def _validate_job_id(value: Any, lineno: int) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(
            f"line {lineno}: job_id must be a non-empty string", line=lineno)
    for ch in value:
        code = ord(ch)
        if code < 0x20 or code == 0x7f:
            raise ManifestError(
                f"line {lineno}: job_id contains a control character",
                line=lineno)
    return value


def read_manifest(path: str) -> list[JobSpec]:
    """Read and validate a UTF-8 JSONL manifest file."""
    try:
        fh = open(path, "r", encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path!r}: {exc}") from exc
    with fh:
        return parse_manifest(fh, source=path)


def parse_manifest(lines, *, source: str = "<manifest>") -> list[JobSpec]:
    """Validate an iterable of JSONL text lines into :class:`JobSpec` list."""
    jobs: list[JobSpec] = []
    seen: set[str] = set()
    for lineno, line in enumerate(lines, start=1):
        text = line.strip()
        if not text:
            raise ManifestError(
                f"{source} line {lineno}: blank lines are not allowed",
                line=lineno)
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ManifestError(
                f"{source} line {lineno}: not valid JSON: {exc}",
                line=lineno) from exc
        if not isinstance(obj, dict):
            raise ManifestError(
                f"{source} line {lineno}: each line must be a JSON object",
                line=lineno)
        if set(obj) != {"job_id", "url"}:
            unknown = sorted(set(obj) - {"job_id", "url"})
            missing = sorted({"job_id", "url"} - set(obj))
            detail = []
            if unknown:
                detail.append(f"unexpected field(s) {unknown}")
            if missing:
                detail.append(f"missing field(s) {missing}")
            raise ManifestError(
                f"{source} line {lineno}: " + "; ".join(detail), line=lineno)
        job_id = _validate_job_id(obj["job_id"], lineno)
        if job_id in seen:
            raise ManifestError(
                f"{source} line {lineno}: duplicate job_id {job_id!r}",
                line=lineno)
        raw_url = obj["url"]
        if not isinstance(raw_url, str) or not raw_url:
            raise ManifestError(
                f"{source} line {lineno}: url must be a non-empty string",
                line=lineno)
        try:
            parsed = parse_url(raw_url)
        except Exception as exc:
            raise ManifestError(
                f"{source} line {lineno}: illegal url {raw_url!r}: {exc}",
                line=lineno) from exc
        seen.add(job_id)
        jobs.append(JobSpec(job_id=job_id, index=len(jobs),
                            raw_url=raw_url, parsed=parsed))
    if not jobs:
        raise ManifestError(f"{source}: manifest contains no jobs")
    return jobs


def config_from_mapping(values: Mapping[str, Any]) -> BatchConfig:
    """Build a :class:`BatchConfig` from a plain JSON-style mapping."""
    if not isinstance(values, Mapping):
        raise BatchConfigError("configuration must be a JSON object")
    unknown = set(values) - {
        "global_inflight", "request_timeout_seconds", "max_body_bytes",
        "max_redirects", "origin_inflight", "origin_min_interval",
        "default_origin", "origin_overrides",
    }
    if unknown:
        raise BatchConfigError(f"unknown configuration key(s): {sorted(unknown)}")

    def _number(name: str, default: float) -> float:
        value = values.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BatchConfigError(f"{name} must be a number, got {value!r}")
        return value

    global_inflight = values.get("global_inflight")
    if global_inflight is None:
        raise BatchConfigError("global_inflight is required")
    if isinstance(global_inflight, bool) or not isinstance(
            global_inflight, int):
        raise BatchConfigError(
            f"global_inflight must be an integer, got {global_inflight!r}")

    default_inflight = values.get("origin_inflight")
    default_interval = _number("origin_min_interval", 1.0)
    if default_inflight is None:
        raise BatchConfigError("origin_inflight (default per-origin cap) is "
                               "required")
    if isinstance(default_inflight, bool) or not isinstance(
            default_inflight, int):
        raise BatchConfigError(
            f"origin_inflight must be an integer, got {default_inflight!r}")

    default_origin = OriginBudget(default_inflight, default_interval)
    overrides: dict[str, OriginBudget] = {}

    # Structured override form: {"origin": {"inflight_cap":..., ...}, ...}
    raw_overrides = values.get("origin_overrides", {})
    if raw_overrides:
        if not isinstance(raw_overrides, Mapping):
            raise BatchConfigError("origin_overrides must be an object")
        for origin, budget in raw_overrides.items():
            if not isinstance(budget, Mapping):
                raise BatchConfigError(
                    f"origin_overrides[{origin!r}] must be an object")
            extra = set(budget) - {"inflight_cap", "min_interval_seconds"}
            if extra:
                raise BatchConfigError(
                    f"origin_overrides[{origin!r}] has unknown keys "
                    f"{sorted(extra)}")
            overrides[origin] = OriginBudget(
                budget.get("inflight_cap", default_inflight),
                budget.get("min_interval_seconds", default_interval))

    # Also accept a full "default_origin" object (convenient for JSON config).
    raw_default = values.get("default_origin")
    if raw_default is not None:
        if not isinstance(raw_default, Mapping):
            raise BatchConfigError("default_origin must be an object")
        extra = set(raw_default) - {"inflight_cap", "min_interval_seconds"}
        if extra:
            raise BatchConfigError(
                f"default_origin has unknown keys {sorted(extra)}")
        default_origin = OriginBudget(
            raw_default.get("inflight_cap", default_inflight),
            raw_default.get("min_interval_seconds", default_interval))

    return BatchConfig(
        global_inflight=global_inflight,
        default_origin=default_origin,
        origin_overrides=overrides,
        request_timeout_seconds=_number(
            "request_timeout_seconds", 30.0),
        max_body_bytes=values.get("max_body_bytes", 4 * 1024 * 1024),
        max_redirects=values.get("max_redirects", 5),
    ).validated()
