"""Result and timeline data models.

Every job ends in exactly one terminal state recorded once:

* ``complete``  -- a final HTTP response (4xx/5xx included) was fully read;
* ``failed``    -- a transport/protocol failure or an illegal redirect;
* ``cancelled`` -- the batch was cancelled before this job (or one of its
  redirect hops) ever started.

The report additionally carries a separate *timeline* describing every
real exchange (start/end), every pacing wait and the cancellation event.
Timeline timestamps come in two flavors: ``mono`` seconds on the runner's
(monotonic, injectable) clock relative to nothing in particular but
mutually comparable, and ``wall`` epoch seconds for human reading.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Terminal outcome labels.
COMPLETE = "complete"
FAILED = "failed"
CANCELLED = "cancelled"


@dataclass
class HopRecord:
    """One request that was actually made as part of one job."""

    hop: int                 # 0-based request number within the job
    url: str                 # canonical address requested
    origin: str
    status: Optional[int]
    location: Optional[str]  # raw Location header (when present)
    body_bytes: int
    body_sha256: Optional[str]
    failure_kind: Optional[str] = None
    failure_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "hop": self.hop,
            "url": self.url,
            "origin": self.origin,
            "status": self.status,
            "location": self.location,
            "body_bytes": self.body_bytes,
            "body_sha256": self.body_sha256,
            "failure_kind": self.failure_kind,
            "failure_reason": self.failure_reason,
        }


@dataclass
class JobResult:
    """The single terminal result of one manifest job."""

    job_id: str
    index: int                    # manifest reception order (0-based)
    original_url: str             # raw spelling from the manifest
    final_url: Optional[str]      # last address requested (canonical)
    outcome: str                  # complete | failed | cancelled
    status: Optional[int] = None  # final HTTP status, when one was obtained
    chain: list[HopRecord] = field(default_factory=list)
    failure_kind: Optional[str] = None
    failure_reason: Optional[str] = None

    @property
    def redirect_chain(self) -> list[str]:
        """Canonical URL of every request made, in request order."""
        return [hop.url for hop in self.chain]

    def to_dict(self) -> dict:
        return {
            "type": "result",
            "job_id": self.job_id,
            "index": self.index,
            "original_url": self.original_url,
            "final_url": self.final_url,
            "outcome": self.outcome,
            "status": self.status,
            "redirect_chain": self.redirect_chain,
            "chain": [hop.to_dict() for hop in self.chain],
            "failure_kind": self.failure_kind,
            "failure_reason": self.failure_reason,
        }


@dataclass
class TimelineEvent:
    """One record in the actual exchange timeline."""

    kind: str            # exchange_start | exchange_end | wait | cancelled
    mono: float
    wall: float
    job_id: Optional[str] = None
    hop: Optional[int] = None
    url: Optional[str] = None
    origin: Optional[str] = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = {
            "type": "timeline",
            "kind": self.kind,
            "mono": round(self.mono, 6),
            "wall": round(self.wall, 6),
        }
        if self.job_id is not None:
            payload["job_id"] = self.job_id
        if self.hop is not None:
            payload["hop"] = self.hop
        if self.url is not None:
            payload["url"] = self.url
        if self.origin is not None:
            payload["origin"] = self.origin
        if self.detail:
            payload["detail"] = self.detail
        return payload
