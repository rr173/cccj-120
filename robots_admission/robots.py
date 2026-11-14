"""robots.txt parsing, User-agent group selection and rule matching.

Only a deliberate subset of the robots grammar is supported:

* fields ``User-agent`` / ``Allow`` / ``Disallow``; field names are
  case-insensitive; unknown fields are ignored;
* the file is UTF-8 bytes with an optional leading BOM, split into
  CR/LF/CRLF lines; everything after ``#`` is a comment;
* adjacent ``User-agent`` lines form one group; the first
  ``Allow``/``Disallow`` opens the group's rule section and a later
  ``User-agent`` starts a new group; blank lines never split adjacent
  ``User-agent`` lines;
* an empty ``Allow``/``Disallow`` is not a rule; a non-empty rule must
  start with ``/`` -- otherwise it is ignored with a line diagnostic;
* group selection: a non-``*`` token matches when it appears
  case-insensitively inside the caller's product token; the longest
  matching token wins and all groups tied at that length are merged;
  the ``*`` groups are used only when no specific token matched, and
  ``*`` restrictions are never mixed into a specific crawler's rules;
* matching: ``*`` matches any-length runs, a trailing ``$`` anchors the
  match at the end of the target, otherwise it is a prefix match;
  competing rules are ordered by normalized byte length (``*`` and the
  trailing ``$`` excluded) and a length tie is won by ``Allow`` --
  never by file order.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import List, Optional

from .normalize import normalize_match_bytes

ALLOW = "allow"
DISALLOW = "disallow"

_LINE_SPLIT = re.compile(r"\r\n|\r|\n")


@dataclass
class Rule:
    """One Allow/Disallow rule with its origin line and match weight."""

    allow: bool
    pattern: bytes      # normalized match bytes ('*' and trailing '$' kept)
    raw: str            # original rule value, comment stripped and trimmed
    line: int           # 1-based line number in the robots file
    specificity: int    # bytes of pattern excluding '*' and trailing '$'

    def to_json(self) -> dict:
        return {
            "allow": self.allow,
            "pattern": self.pattern.decode("ascii"),
            "raw": self.raw,
            "line": self.line,
            "specificity": self.specificity,
        }

    @staticmethod
    def from_json(data: dict) -> "Rule":
        return Rule(bool(data["allow"]), data["pattern"].encode("ascii"),
                    data["raw"], int(data["line"]), int(data["specificity"]))


@dataclass
class Group:
    """One User-agent group: the agent tokens plus their shared rules."""

    agents: List[str] = field(default_factory=list)
    rules: List[Rule] = field(default_factory=list)


@dataclass
class ParsedPolicy:
    """Parsed robots.txt: groups plus line-level diagnostics."""

    groups: List[Group] = field(default_factory=list)
    diagnostics: List[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({
            "groups": [{"agents": g.agents,
                        "rules": [r.to_json() for r in g.rules]}
                       for g in self.groups],
            "diagnostics": self.diagnostics,
        }, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def from_json(payload: str) -> "ParsedPolicy":
        data = json.loads(payload)
        return ParsedPolicy(
            [Group(list(g["agents"]), [Rule.from_json(r) for r in g["rules"]])
             for g in data["groups"]],
            list(data["diagnostics"]))


def parse_robots(body: bytes) -> ParsedPolicy:
    """Parse robots.txt bytes (UTF-8, optional leading BOM).

    Raises ``UnicodeDecodeError`` on invalid UTF-8; the caller must
    reject the fetch result in that case and keep any old policy.
    """
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError(f"robots body must be bytes, got {type(body).__name__}")
    text = bytes(body).decode("utf-8-sig")
    groups: List[Group] = []
    diagnostics: List[str] = []
    agents: List[str] = []
    rules: List[Rule] = []
    in_rules = False

    def flush() -> None:
        nonlocal agents, rules, in_rules
        if agents or rules:
            groups.append(Group(agents, rules))
        agents, rules, in_rules = [], [], False

    for line_no, raw_line in enumerate(_LINE_SPLIT.split(text), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        name, _, value = line.partition(":")
        name = name.strip().lower()
        value = value.strip()
        if name == "user-agent":
            if not value:
                diagnostics.append(f"第 {line_no} 行：空 User-agent 已忽略")
                continue
            if in_rules:
                flush()
            agents.append(value)
        elif name in ("allow", "disallow"):
            in_rules = True
            allow = name == "allow"
            if not value:
                diagnostics.append(f"第 {line_no} 行：空 {name} 不构成规则，已忽略")
                continue
            if not value.startswith("/"):
                diagnostics.append(
                    f"第 {line_no} 行：{name} 的值 {value!r} 未以 '/' 开头，已忽略")
                continue
            try:
                pattern = normalize_match_bytes(value)
            except ValueError as exc:
                diagnostics.append(
                    f"第 {line_no} 行：{name} 的值 {value!r} 已忽略（{exc}）")
                continue
            specificity = (len(pattern.replace(b"*", b""))
                           - (1 if pattern.endswith(b"$") else 0))
            rules.append(Rule(allow, pattern, value, line_no, specificity))
        # unknown fields are ignored silently
    flush()
    return ParsedPolicy(groups, diagnostics)


def rule_matches(pattern: bytes, target: bytes) -> bool:
    """Match a normalized rule pattern against normalized target bytes."""
    anchored = pattern.endswith(b"$")
    if anchored:
        pattern = pattern[:-1]
    segments = pattern.split(b"*")
    if len(segments) == 1:
        return target == segments[0] if anchored else target.startswith(segments[0])
    if not target.startswith(segments[0]):
        return False
    pos = len(segments[0])
    last = len(segments) - 1
    for idx in range(1, last + 1):
        seg = segments[idx]
        if idx == last and anchored:
            start = len(target) - len(seg)
            return start >= pos and target[start:] == seg
        found = target.find(seg, pos)
        if found < 0:
            return False
        pos = found + len(seg)
    return True


@dataclass
class Evaluation:
    """The rule-level verdict for one (policy, token, target) triple."""

    outcome: str                  # ALLOW | DISALLOW
    reason: str                   # human-readable explanation (Chinese)
    selection: str                # 'specific' | 'wildcard' | 'none'
    group_agents: List[str]
    matched_agent: Optional[str]
    rule: Optional[Rule]
    hits: int = 0
    tie_broken: bool = False      # a length tie was won by Allow


def evaluate(policy: ParsedPolicy, token: str, target: bytes) -> Evaluation:
    """Select the applicable group(s) for ``token`` and judge ``target``."""
    lowered = token.lower()
    best_len = 0
    best_groups: List[Group] = []
    star_groups: List[Group] = []
    for group in policy.groups:
        group_best = 0
        group_star = False
        for agent in group.agents:
            if agent == "*":
                group_star = True
            elif agent.lower() in lowered:
                group_best = max(group_best, len(agent))
        if group_best:
            if group_best > best_len:
                best_len, best_groups = group_best, [group]
            elif group_best == best_len:
                best_groups.append(group)
        elif group_star:
            star_groups.append(group)

    if best_groups:
        selection, groups = "specific", best_groups
        matched_agent = max(
            (a for g in groups for a in g.agents
             if a != "*" and a.lower() in lowered),
            key=len, default=None)
    else:
        selection, groups = "wildcard", star_groups
        matched_agent = "*" if groups else None

    agents = [a for g in groups for a in g.agents]
    if not groups:
        return Evaluation(
            ALLOW,
            f"没有适用于采集器 {token!r} 的 User-agent 组，robots 不限制该采集器",
            "none", [], None, None)
    if selection == "specific":
        sel = (f"按最长匹配 token（长度 {best_len}）合并 {len(groups)} 个同长度组 {agents}"
               if len(groups) > 1 else
               f"按具体 token {matched_agent!r} 选中组 {agents}")
    else:
        sel = f"没有具体 token 匹配，改用 * 组 {agents}"

    rules = [r for g in groups for r in g.rules]
    if not rules:
        return Evaluation(ALLOW, f"{sel}；组内没有任何规则",
                          selection, agents, matched_agent, None)
    hits = [r for r in rules if rule_matches(r.pattern, target)]
    if not hits:
        return Evaluation(
            ALLOW,
            f"{sel}；组内 {len(rules)} 条规则均未命中匹配对象 "
            f"{target.decode('ascii')}",
            selection, agents, matched_agent, None)
    top = max(r.specificity for r in hits)
    winners = [r for r in hits if r.specificity == top]
    allows = [r for r in winners if r.allow]
    tie_broken = 0 < len(allows) < len(winners)
    chosen = min(allows or winners, key=lambda r: r.line)
    outcome = ALLOW if chosen.allow else DISALLOW
    effect = "Allow" if chosen.allow else "Disallow"
    reason = (f"{sel}；命中第 {chosen.line} 行 {chosen.raw!r}"
              f"（{effect}，有效长度 {chosen.specificity} 字节")
    if len(hits) > 1:
        reason += f"，共 {len(hits)} 条命中取最长"
    if tie_broken:
        reason += "，同长度平局按 Allow 优先"
    reason += "）"
    return Evaluation(outcome, reason, selection, agents, matched_agent,
                      chosen, hits=len(hits), tie_broken=tie_broken)
