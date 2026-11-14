"""Regression tests for robots_admission (offline, stdlib + SQLite)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from robots_admission import (
    ALLOW,
    DISALLOW,
    NEEDS_REFRESH,
    InvalidParameterError,
    InvalidURLError,
    RobotsCache,
    evaluate,
    normalize_match_bytes,
    normalize_url,
    parse_origin,
    parse_robots,
    rule_matches,
)

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


# ----------------------------------------------------------------------
# origin / URL normalization
# ----------------------------------------------------------------------

def test_origin_normalizes_case_and_default_port():
    assert parse_origin("HTTP://EXAMPLE.com:80/a").key == "http://example.com"
    assert parse_origin("https://EXAMPLE.com:443").key == "https://example.com"


def test_origin_keeps_non_default_port_and_separates_schemes():
    assert parse_origin("http://example.com:8080/").key == "http://example.com:8080"
    assert parse_origin("http://example.com/").key != parse_origin("https://example.com/").key


def test_origin_idna_and_ipv6():
    assert parse_origin("http://bücher.de/").key == "http://xn--bcher-kva.de"
    assert parse_origin("http://[2001:0DB8::1]:8080/").key == "http://[2001:db8::1]:8080"


@pytest.mark.parametrize("url", [
    "ftp://example.com/x",          # other scheme
    "http://user:pw@example.com/",  # userinfo
    "http:///x",                    # no host
    "http://example.com:bad/x",     # non-numeric port
    "http://example.com:99999/x",   # port out of range
    "example.com/x",                # no scheme
])
def test_url_rejections(url):
    with pytest.raises(InvalidURLError):
        normalize_url(url)


def test_match_target_empty_path_query_and_fragment():
    _, target = normalize_url("http://example.com")
    assert target == b"/"
    _, target = normalize_url("http://example.com/p?q=1#frag")
    assert target == b"/p?q=1"


def test_percent_normalization_rules():
    assert normalize_match_bytes("/%7Eold") == b"/~old"          # unreserved decoded
    assert normalize_match_bytes("/%2f") == b"/%2F"              # kept, hex uppercased
    assert normalize_match_bytes("/中") == b"/%E4%B8%AD"         # UTF-8 bytes
    assert normalize_match_bytes("/a%2Fb") == b"/a%2Fb"          # path case preserved
    with pytest.raises(InvalidURLError):
        normalize_match_bytes("/bad%zz")
    with pytest.raises(InvalidURLError):
        normalize_match_bytes("/bad%4")


def test_encoded_slash_is_not_a_separator():
    _, target = normalize_url("http://example.com/files/%2Fsecret")
    assert target == b"/files/%2Fsecret"
    assert target != b"/files//secret"


# ----------------------------------------------------------------------
# robots parsing
# ----------------------------------------------------------------------

def test_parse_groups_blank_lines_and_comments():
    policy = parse_robots(
        b"# c\r\nUser-agent: a # tail\nUser-agent: b\n\nUser-agent: c\n"
        b"Disallow: /x\nUser-agent: d\nDisallow: /y\n")
    assert [g.agents for g in policy.groups] == [["a", "b", "c"], ["d"]]
    assert policy.groups[0].rules[0].raw == "/x"
    assert policy.groups[1].rules[0].raw == "/y"


def test_parse_bom_crlf_and_case_insensitive_fields():
    policy = parse_robots(b"\xef\xbb\xbfUSER-AGENT: a\r\nDISALLOW: /x\r\n")
    assert policy.groups[0].agents == ["a"]
    assert policy.groups[0].rules[0].allow is False


def test_parse_diagnostics_for_bad_rules():
    policy = parse_robots(
        b"User-agent: a\nDisallow:\nAllow: no-slash\nDisallow: /bad%zz\n"
        b"Unknown-Field: 1\n")
    assert policy.groups[0].rules == []
    assert len(policy.diagnostics) == 3  # empty, no '/', bad percent
    assert not any("Unknown" in d for d in policy.diagnostics)


def test_parse_invalid_utf8_raises():
    with pytest.raises(UnicodeDecodeError):
        parse_robots(b"\xff\xfe")


# ----------------------------------------------------------------------
# group selection and matching
# ----------------------------------------------------------------------

def _policy(text: str):
    return parse_robots(text.encode("utf-8"))


def test_specific_beats_star_and_star_never_mixed():
    policy = _policy("User-agent: Bot\nDisallow: /a\n\nUser-agent: *\nDisallow: /b\n")
    ev = evaluate(policy, "Bot", b"/b")
    assert ev.outcome == ALLOW and ev.selection == "specific"  # /b not in Bot's group
    ev = evaluate(policy, "Other", b"/b")
    assert ev.outcome == DISALLOW and ev.selection == "wildcard"


def test_longest_token_wins_and_equal_length_groups_merge():
    policy = _policy(
        "User-agent: Ab\nDisallow: /x\n\n"
        "User-agent: Cd\nAllow: /x\n")
    ev = evaluate(policy, "AbCd", b"/x")   # both length-2 tokens match -> merge
    assert ev.outcome == ALLOW and ev.tie_broken
    ev = evaluate(policy, "Ab", b"/x")
    assert ev.outcome == DISALLOW


def test_token_matching_is_case_insensitive_substring():
    policy = _policy("User-agent: AlphaBot\nDisallow: /x\n")
    assert evaluate(policy, "my-ALPHAbot-2", b"/x").outcome == DISALLOW


def test_rule_matching_star_dollar_prefix():
    assert rule_matches(b"/a*b", b"/aXXb")
    assert not rule_matches(b"/a*b$", b"/aXXb/c")
    assert rule_matches(b"/a$", b"/a")
    assert not rule_matches(b"/a$", b"/ab")
    assert rule_matches(b"/a", b"/ab")  # prefix semantics


def test_longest_specificity_then_allow_not_file_order():
    policy = _policy(
        "User-agent: b\nAllow: /p\nDisallow: /p\n")  # Allow first in file
    ev = evaluate(policy, "b", b"/p")
    assert ev.outcome == ALLOW and ev.tie_broken


def test_percent_rules_count_three_bytes():
    policy = _policy("User-agent: b\nDisallow: /%2Fx\nAllow: /%2F\n")
    # '/%2Fx' is 5 bytes, '/%2F' is 4 bytes after normalization
    ev = evaluate(policy, "b", b"/%2Fx")
    assert ev.outcome == DISALLOW


# ----------------------------------------------------------------------
# cache semantics
# ----------------------------------------------------------------------

def feed(cache, url, t, status, ttl, body=None, etag=None):
    return cache.update(url, fetched_at=t, status=status, ttl_seconds=ttl,
                        body=body, etag=etag)


def test_200_then_check_and_persistence(tmp_path):
    db = str(tmp_path / "c.db")
    with RobotsCache(db) as c:
        assert feed(c, "http://e.com/", at(0), 200, 100,
                    body=b"User-agent: *\nDisallow: /x\n").accepted
    with RobotsCache(db) as c:  # reopen: same judgment basis
        assert c.check("http://e.com/x", "b", at(50)).outcome == DISALLOW
        assert c.check("http://e.com/y", "b", at(50)).outcome == ALLOW
        assert c.check("http://e.com/x", "b", at(100)).outcome == NEEDS_REFRESH


def test_expiry_boundary_is_exclusive():
    with RobotsCache(":memory:") as c:
        feed(c, "http://e.com/", at(0), 200, 10, body=b"User-agent: *\nDisallow: /x\n")
        assert c.check("http://e.com/x", "b", at(9)).outcome == DISALLOW
        assert c.check("http://e.com/x", "b", at(10)).outcome == NEEDS_REFRESH


def test_empty_200_and_404_410_are_unrestricted():
    with RobotsCache(":memory:") as c:
        feed(c, "http://a.com/", at(0), 200, 10, body=b"")
        feed(c, "http://b.com/", at(0), 404, 10)
        feed(c, "http://c.com/", at(0), 410, 10)
        assert c.check("http://a.com/x", "b", at(5)).outcome == ALLOW
        assert c.check("http://b.com/x", "b", at(5)).outcome == ALLOW
        assert c.check("http://c.com/x", "b", at(5)).outcome == ALLOW
        assert c.check("http://b.com/x", "b", at(10)).outcome == NEEDS_REFRESH


def test_304_refresh_requires_prior_200_and_matching_etag():
    with RobotsCache(":memory:") as c:
        assert not feed(c, "http://e.com/", at(0), 304, 10, etag="v1").accepted
        feed(c, "http://e.com/", at(0), 200, 10,
             body=b"User-agent: *\nDisallow: /x\n", etag="v1")
        assert not feed(c, "http://e.com/", at(5), 304, 100, etag="nope").accepted
        assert feed(c, "http://e.com/", at(5), 304, 100, etag="v1").accepted
        d = c.check("http://e.com/x", "b", at(50))
        assert d.outcome == DISALLOW and d.cache.status == 304
        assert c.check("http://e.com/x", "b", at(105)).outcome == NEEDS_REFRESH


def test_stale_or_equal_updates_are_rejected():
    with RobotsCache(":memory:") as c:
        feed(c, "http://e.com/", at(10), 200, 100, body=b"User-agent: *\nDisallow: /x\n")
        assert not feed(c, "http://e.com/", at(10), 200, 100).accepted  # equal
        assert not feed(c, "http://e.com/", at(5), 404, 100).accepted   # stale
        assert c.check("http://e.com/x", "b", at(20)).outcome == DISALLOW


def test_429_and_5xx_record_failure_without_touching_policy():
    with RobotsCache(":memory:") as c:
        feed(c, "http://e.com/", at(0), 200, 100, body=b"User-agent: *\nDisallow: /x\n")
        assert not feed(c, "http://e.com/", at(50), 429, 100).accepted
        assert not feed(c, "http://e.com/", at(60), 503, 100).accepted
        assert c.check("http://e.com/x", "b", at(70)).outcome == DISALLOW   # old valid
        assert c.check("http://e.com/x", "b", at(100)).outcome == NEEDS_REFRESH
        assert [f["status"] for f in c.failures("http://e.com/")] == [429, 503]
        # a later valid result recovers (failures did not advance the clock)
        assert feed(c, "http://e.com/", at(70), 200, 100,
                    body=b"User-agent: *\nDisallow: /y\n").accepted
        assert c.check("http://e.com/x", "b", at(80)).outcome == ALLOW


def test_bad_content_never_replaces_valid_policy():
    with RobotsCache(":memory:") as c:
        feed(c, "http://e.com/", at(0), 200, 1000, body=b"User-agent: *\nDisallow: /x\n")
        assert not feed(c, "http://e.com/", at(10), 200, 1000, body=b"\xff\xfe").accepted
        assert not feed(c, "http://e.com/", at(20), 200, 1000,
                        body=b"a" * (512 * 1024 + 1)).accepted
        d = c.check("http://e.com/x", "b", at(30))
        assert d.outcome == DISALLOW and d.cache.fetched_at == at(0).isoformat()


def test_other_status_rejected():
    with RobotsCache(":memory:") as c:
        assert not feed(c, "http://e.com/", at(0), 301, 10).accepted
        assert c.check("http://e.com/x", "b", at(1)).outcome == NEEDS_REFRESH


def test_invalid_parameters_raise_and_keep_cache():
    with RobotsCache(":memory:") as c:
        feed(c, "http://e.com/", at(0), 200, 100, body=b"User-agent: *\nDisallow: /x\n")
        naive = datetime(2026, 1, 1)
        with pytest.raises(InvalidParameterError):
            feed(c, "http://e.com/", naive, 200, 10)
        with pytest.raises(InvalidParameterError):
            feed(c, "http://e.com/", at(1), 200, -1)
        with pytest.raises(TypeError):
            feed(c, "http://e.com/", at(1), 200, 10, body="str")
        with pytest.raises(InvalidParameterError):
            c.check("http://e.com/x", "b", naive)
        with pytest.raises(InvalidParameterError):
            c.check("http://e.com/x", "", at(1))
        with pytest.raises(InvalidParameterError):
            c.check("http://e.com/x", "two tokens", at(1))
        assert c.check("http://e.com/x", "b", at(1)).outcome == DISALLOW


def test_needs_refresh_is_not_permission():
    with RobotsCache(":memory:") as c:
        d = c.check("http://never.example/x", "b", at(0))
        assert d.outcome == NEEDS_REFRESH and d.cache is None
