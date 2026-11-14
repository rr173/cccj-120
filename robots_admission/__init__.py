"""robots_admission — explainable robots.txt crawl-admission module.

A scheduler calls this package *before* issuing any request:

* :meth:`RobotsCache.update` advances one origin's cached policy with a
  fetch result (in the offline drill the caller feeds local bytes; the
  module itself never touches the network);
* :meth:`RobotsCache.check` answers ``allow`` / ``disallow`` /
  ``needs_refresh`` for a (URL, product token, aware instant) triple.

``needs_refresh`` means there is no usable policy right now: the
scheduler must defer the URL and must not read it as permission.

The cache lives in a single SQLite file and is the only authority for
decisions -- run-event ledgers or WARC fetch facts are never consulted.
Only the Python standard library is used.

Public API:
    RobotsCache            open/create the SQLite cache; update + check
    Decision               one admission answer (see .to_dict())
    UpdateResult           outcome of one cache update
    CacheView, RuleHit     decision detail records
    parse_robots           parse robots.txt bytes -> ParsedPolicy
    evaluate               rule-level verdict for (policy, token, target)
    normalize_url          URL -> (Origin, match-target bytes)
    parse_origin           URL -> Origin (cache key)
    normalize_match_bytes  percent-normalization used for matching
    render_decision        human-readable rendering of a Decision
    parse_iso_instant      CLI helper for timezone-required ISO-8601
"""

from __future__ import annotations

from .cache import (
    ALLOW,
    DISALLOW,
    MAX_ROBOTS_BYTES,
    NEEDS_REFRESH,
    CacheView,
    Decision,
    RobotsCache,
    RuleHit,
    UpdateResult,
    render_decision,
)
from .errors import AdmissionError, InvalidParameterError, InvalidURLError
from .normalize import Origin, normalize_match_bytes, normalize_url, parse_origin
from .robots import Evaluation, Group, ParsedPolicy, Rule, evaluate, parse_robots, rule_matches
from .validate import parse_iso_instant, require_aware_datetime, require_product_token

__all__ = [
    "RobotsCache",
    "Decision",
    "UpdateResult",
    "CacheView",
    "RuleHit",
    "ALLOW",
    "DISALLOW",
    "NEEDS_REFRESH",
    "MAX_ROBOTS_BYTES",
    "AdmissionError",
    "InvalidURLError",
    "InvalidParameterError",
    "Origin",
    "normalize_url",
    "normalize_match_bytes",
    "parse_origin",
    "parse_robots",
    "evaluate",
    "rule_matches",
    "ParsedPolicy",
    "Group",
    "Rule",
    "Evaluation",
    "render_decision",
    "parse_iso_instant",
    "require_aware_datetime",
    "require_product_token",
]

__version__ = "1.0.0"
