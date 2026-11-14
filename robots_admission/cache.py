"""SQLite-backed per-origin robots policy cache and admission decisions.

The cache is the *only* authority for admission decisions: run-event
ledgers or WARC fetch facts are never consulted.  Every fetch result is
applied as one complete, transactional update (time checks, parsing and
replacement), so a reopened database keeps the exact same judgment
basis.  No machine clock is ever read: every instant comes from the
caller.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from .errors import InvalidParameterError
from .normalize import normalize_url, parse_origin
from .robots import ALLOW, DISALLOW, ParsedPolicy, evaluate, parse_robots
from .validate import require_aware_datetime, require_product_token

NEEDS_REFRESH = "needs_refresh"

MAX_ROBOTS_BYTES = 512 * 1024  # 512 KiB

_SCHEMA = """
CREATE TABLE IF NOT EXISTS policies (
    origin           TEXT PRIMARY KEY,  -- normalized origin key
    kind             TEXT NOT NULL,     -- 'rules' | 'unrestricted'
    status           INTEGER NOT NULL,  -- status of the last accepted fetch
    fetched_at       TEXT NOT NULL,     -- original ISO-8601 instant
    fetched_at_epoch REAL NOT NULL,     -- same instant, for comparisons
    ttl_seconds      INTEGER NOT NULL,
    etag             TEXT,
    robots           BLOB,              -- raw bytes for kind='rules'
    parsed_json      TEXT               -- parsed policy for kind='rules'
);
CREATE TABLE IF NOT EXISTS refresh_failures (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    origin   TEXT NOT NULL,
    at       TEXT NOT NULL,
    at_epoch REAL NOT NULL,
    status   INTEGER NOT NULL
);
"""


def _iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


@dataclass
class CacheView:
    """The cache representation a decision was based on."""

    kind: str             # 'rules' | 'unrestricted'
    status: int           # status of the last accepted fetch (200/404/410/304)
    fetched_at: str
    valid_until: str      # exclusive upper bound of the validity window
    ttl_seconds: int
    etag: Optional[str]
    fresh: bool           # whether the window covers the decision instant

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "status": self.status,
            "fetched_at": self.fetched_at,
            "valid_until": self.valid_until,
            "ttl_seconds": self.ttl_seconds,
            "etag": self.etag,
            "fresh": self.fresh,
        }


@dataclass
class RuleHit:
    """The rule that decided an allow/disallow outcome."""

    effect: str           # 'allow' | 'disallow'
    raw: str              # original rule text
    line: int             # 1-based line number in the robots file
    pattern: str          # normalized match bytes (ASCII)
    specificity: int      # byte length without '*' and trailing '$'

    def to_dict(self) -> dict:
        return {
            "effect": self.effect,
            "raw": self.raw,
            "line": self.line,
            "pattern": self.pattern,
            "specificity": self.specificity,
        }


@dataclass
class Decision:
    """The admission answer for one (URL, token, instant) triple."""

    outcome: str                     # allow | disallow | needs_refresh
    url: str
    origin: str                      # normalized origin key
    token: str
    now: str
    target: str                      # normalized match target (ASCII)
    reason: str                      # human-readable explanation (Chinese)
    cache: Optional[CacheView]       # cache representation used, if any
    group_agents: List[str] = field(default_factory=list)
    selection: str = "none"          # 'specific' | 'wildcard' | 'none'
    matched_agent: Optional[str] = None
    rule: Optional[RuleHit] = None   # the deciding rule, if one matched
    diagnostics: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "url": self.url,
            "origin": self.origin,
            "token": self.token,
            "now": self.now,
            "target": self.target,
            "reason": self.reason,
            "cache": None if self.cache is None else self.cache.to_dict(),
            "group": {
                "agents": self.group_agents,
                "selection": self.selection,
                "matched_agent": self.matched_agent,
            },
            "rule": None if self.rule is None else self.rule.to_dict(),
            "diagnostics": self.diagnostics,
        }


@dataclass
class UpdateResult:
    """The outcome of feeding one fetch result into the cache."""

    accepted: bool
    origin: str
    status: int
    reason: str
    fetched_at: Optional[str] = None
    valid_until: Optional[str] = None
    diagnostics: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "origin": self.origin,
            "status": self.status,
            "reason": self.reason,
            "fetched_at": self.fetched_at,
            "valid_until": self.valid_until,
            "diagnostics": self.diagnostics,
        }


class RobotsCache:
    """Per-origin robots policy cache stored in a single SQLite file."""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        parent = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "RobotsCache":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # cache updates
    # ------------------------------------------------------------------

    def update(self, url: str, *, fetched_at: datetime, status: int,
               ttl_seconds: int, body: Optional[bytes] = None,
               etag: Optional[str] = None) -> UpdateResult:
        """Feed one fetch result for the origin of ``url``.

        The time checks, parsing and cache replacement happen as one
        complete transaction.  Rejected results never modify the cache.
        """
        fetched_at = require_aware_datetime(fetched_at, "fetched_at")
        if isinstance(status, bool) or not isinstance(status, int):
            raise TypeError(f"status must be int, got {type(status).__name__}")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise TypeError(
                f"ttl_seconds must be int, got {type(ttl_seconds).__name__}")
        if ttl_seconds < 0:
            raise InvalidParameterError("ttl_seconds must be a non-negative integer")
        if body is not None and not isinstance(body, (bytes, bytearray)):
            raise TypeError(f"body must be bytes or None, got {type(body).__name__}")
        if etag is not None and not isinstance(etag, str):
            raise TypeError(f"etag must be str or None, got {type(etag).__name__}")
        origin = parse_origin(url)
        epoch = fetched_at.timestamp()
        iso = fetched_at.isoformat()

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT * FROM policies WHERE origin = ?",
                (origin.key,)).fetchone()
            result = self._apply(row, origin.key, iso, epoch, status,
                                 ttl_seconds,
                                 None if body is None else bytes(body), etag)
            self._conn.execute("COMMIT")
        except BaseException:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return result

    def _apply(self, row, key: str, iso: str, epoch: float, status: int,
               ttl: int, body: Optional[bytes],
               etag: Optional[str]) -> UpdateResult:
        # 429 / 5xx: record the failed refresh only -- the old
        # representation is neither extended nor deleted, and the
        # monotonic clock of *accepted* results does not advance.
        if status == 429 or 500 <= status <= 599:
            self._conn.execute(
                "INSERT INTO refresh_failures (origin, at, at_epoch, status)"
                " VALUES (?, ?, ?, ?)", (key, iso, epoch, status))
            return UpdateResult(
                False, key, status,
                "刷新失败已记录；不延长旧策略有效期，也不删除旧表示")

        if status not in (200, 304, 404, 410):
            return UpdateResult(
                False, key, status,
                f"拒绝：本模块不支持的状态码 {status}，缓存不变")

        # Monotonicity: every accepted update must be strictly later
        # than the last accepted fetch result for this origin, so a late
        # refresh can never overwrite a newer policy.
        if row is not None and epoch <= row["fetched_at_epoch"]:
            return UpdateResult(
                False, key, status,
                f"拒绝：fetched_at={iso} 未严格晚于上次已接受的 "
                f"{row['fetched_at']}，缓存不变")

        if status == 200:
            payload = body if body is not None else b""
            if len(payload) > MAX_ROBOTS_BYTES:
                return UpdateResult(
                    False, key, status,
                    f"拒绝：robots 内容 {len(payload)} 字节超过 512 KiB 上限，"
                    "缓存不变")
            try:
                policy = parse_robots(payload)
            except UnicodeDecodeError as exc:
                return UpdateResult(
                    False, key, status,
                    f"拒绝：robots 不是有效 UTF-8（{exc}），缓存不变")
            self._replace(key, "rules", status, iso, epoch, ttl, etag,
                          payload, policy.to_json())
            return UpdateResult(
                True, key, status, "已建立/替换该 origin 的规则策略",
                iso, _iso_utc(epoch + ttl), policy.diagnostics)

        if status in (404, 410):
            self._replace(key, "unrestricted", status, iso, epoch, ttl,
                          None, None, None)
            return UpdateResult(
                True, key, status,
                "已建立有到期时间的无规则表示（视为不限制任何路径）",
                iso, _iso_utc(epoch + ttl), [])

        # status == 304: only refreshes the validity window of an
        # existing 200 representation whose ETag matches; the original
        # robots bytes and rules are kept.  Never fabricates a policy.
        if row is None or row["kind"] != "rules":
            return UpdateResult(
                False, key, status,
                "拒绝：没有可刷新的 200 表示，缓存不变")
        if etag != row["etag"]:
            return UpdateResult(
                False, key, status,
                f"拒绝：ETag 校验不符（缓存 {row['etag']!r}，收到 {etag!r}），"
                "缓存不变")
        self._conn.execute(
            "UPDATE policies SET status = ?, fetched_at = ?,"
            " fetched_at_epoch = ?, ttl_seconds = ? WHERE origin = ?",
            (status, iso, epoch, ttl, key))
        return UpdateResult(
            True, key, status,
            "已用 304 刷新有效期，沿用原 robots 字节与规则",
            iso, _iso_utc(epoch + ttl), [])

    def _replace(self, key: str, kind: str, status: int, iso: str,
                 epoch: float, ttl: int, etag: Optional[str],
                 payload: Optional[bytes], parsed_json: Optional[str]) -> None:
        self._conn.execute(
            "INSERT INTO policies (origin, kind, status, fetched_at,"
            " fetched_at_epoch, ttl_seconds, etag, robots, parsed_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(origin) DO UPDATE SET"
            " kind = excluded.kind, status = excluded.status,"
            " fetched_at = excluded.fetched_at,"
            " fetched_at_epoch = excluded.fetched_at_epoch,"
            " ttl_seconds = excluded.ttl_seconds, etag = excluded.etag,"
            " robots = excluded.robots, parsed_json = excluded.parsed_json",
            (key, kind, status, iso, epoch, ttl, etag, payload, parsed_json))

    # ------------------------------------------------------------------
    # admission decisions
    # ------------------------------------------------------------------

    def check(self, url: str, token: str, now: datetime) -> Decision:
        """Decide allow / disallow / needs_refresh for one candidate URL.

        ``needs_refresh`` means there is no usable policy right now; the
        scheduler must defer the URL and must not read it as permission.
        """
        token = require_product_token(token)
        now = require_aware_datetime(now, "now")
        origin, target = normalize_url(url)
        now_iso = now.isoformat()
        target_text = target.decode("ascii")
        row = self._conn.execute(
            "SELECT * FROM policies WHERE origin = ?",
            (origin.key,)).fetchone()
        if row is None:
            return Decision(
                NEEDS_REFRESH, url, origin.key, token, now_iso, target_text,
                "该 origin 没有任何缓存策略；调度器必须暂缓此 URL"
                "（needs_refresh 不是许可）",
                None)
        valid_until_epoch = row["fetched_at_epoch"] + row["ttl_seconds"]
        fresh = row["fetched_at_epoch"] <= now.timestamp() < valid_until_epoch
        view = CacheView(row["kind"], row["status"], row["fetched_at"],
                         _iso_utc(valid_until_epoch), row["ttl_seconds"],
                         row["etag"], fresh)
        if not fresh:
            return Decision(
                NEEDS_REFRESH, url, origin.key, token, now_iso, target_text,
                f"缓存策略已于 {view.valid_until} 到期（有效区间为 "
                "fetched_at <= now < fetched_at + ttl）；调度器必须暂缓此 URL",
                view, diagnostics=self._diagnostics_of(row))
        if row["kind"] == "unrestricted":
            return Decision(
                ALLOW, url, origin.key, token, now_iso, target_text,
                f"缓存表示来自 HTTP {row['status']}（无规则表示），不限制任何路径",
                view)
        policy = ParsedPolicy.from_json(row["parsed_json"])
        if not policy.groups:
            return Decision(
                ALLOW, url, origin.key, token, now_iso, target_text,
                "robots 为空或不含任何规则组，不限制任何路径",
                view, diagnostics=policy.diagnostics)
        ev = evaluate(policy, token, target)
        hit = None
        if ev.rule is not None:
            hit = RuleHit("allow" if ev.rule.allow else "disallow",
                          ev.rule.raw, ev.rule.line,
                          ev.rule.pattern.decode("ascii"),
                          ev.rule.specificity)
        return Decision(ev.outcome, url, origin.key, token, now_iso,
                        target_text, ev.reason, view, ev.group_agents,
                        ev.selection, ev.matched_agent, hit,
                        policy.diagnostics)

    @staticmethod
    def _diagnostics_of(row) -> List[str]:
        if row["kind"] == "rules" and row["parsed_json"]:
            return ParsedPolicy.from_json(row["parsed_json"]).diagnostics
        return []

    # ------------------------------------------------------------------
    # introspection (never used for decisions)
    # ------------------------------------------------------------------

    def origins(self) -> List[str]:
        """All origins with a cached representation."""
        return [r["origin"] for r in self._conn.execute(
            "SELECT origin FROM policies ORDER BY origin")]

    def failures(self, origin: Optional[str] = None) -> List[dict]:
        """Recorded 429/5xx refresh failures (observability only)."""
        if origin is not None:
            key = parse_origin(origin).key
            rows = self._conn.execute(
                "SELECT * FROM refresh_failures WHERE origin = ?"
                " ORDER BY id", (key,))
        else:
            rows = self._conn.execute(
                "SELECT * FROM refresh_failures ORDER BY id")
        return [{"origin": r["origin"], "at": r["at"], "status": r["status"]}
                for r in rows]


def render_decision(d: Decision) -> str:
    """Human-readable multi-line rendering of one Decision."""
    lines = [f"[{d.outcome}] {d.url}  (agent={d.token}, now={d.now})",
             f"  origin: {d.origin}",
             f"  target: {d.target}"]
    if d.cache is not None:
        c = d.cache
        lines.append(
            f"  cache: kind={c.kind} status={c.status}"
            f" fetched_at={c.fetched_at} ttl={c.ttl_seconds}s"
            f" valid_until={c.valid_until} etag={c.etag} fresh={c.fresh}")
    else:
        lines.append("  cache: <无缓存表示>")
    if d.group_agents:
        lines.append(
            f"  group: agents={d.group_agents} selection={d.selection}"
            f" matched={d.matched_agent}")
    if d.rule is not None:
        r = d.rule
        lines.append(
            f"  rule: 第 {r.line} 行 {r.raw!r} -> {r.effect}"
            f" (pattern={r.pattern!r}, 有效长度={r.specificity})")
    lines.append(f"  reason: {d.reason}")
    for diag in d.diagnostics:
        lines.append(f"  diag: {diag}")
    return "\n".join(lines)
