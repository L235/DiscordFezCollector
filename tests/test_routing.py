"""Characterization tests for the pure routing pipeline (src/routing.py).

These pin the current routing behavior — which events are fast-rejected,
which filters match, how messages are truncated, and how timestamps are
parsed — so later structural refactors cannot silently change it.
"""

from datetime import UTC, datetime

from src.constants import DISCORD_MESSAGE_LIMIT, TRUNCATED_MESSAGE_SUFFIX
from src.event_streams import _event_ts_to_epoch
from src.routing import fast_reject, match_filters, truncate_for_discord
from tests.factories import make_change, make_filter

# ── fast_reject ───────────────────────────────────────────────────────────── #

def test_fast_reject_no_filters_rejects_everything():
    assert fast_reject(make_change(), [])


def test_fast_reject_passes_whitelisted_user():
    filt = make_filter(userIncludeList=["Alice"])
    assert not fast_reject(make_change(), [filt])
    assert fast_reject(make_change(user="Bob"), [filt])


def test_fast_reject_passes_page_include_match():
    filt = make_filter(pageIncludePatterns=["Sandb"])
    assert not fast_reject(make_change(), [filt])
    assert fast_reject(make_change(title="Other page"), [filt])


def test_fast_reject_passes_summary_include_match():
    filt = make_filter(summaryIncludePatterns=["revert"])
    assert not fast_reject(make_change(comment="Reverted vandalism"), [filt])
    assert fast_reject(make_change(), [filt])


def test_fast_reject_prefers_log_action_comment():
    # Log events carry the routable text in log_action_comment.
    filt = make_filter(summaryIncludePatterns=["blocked"])
    change = make_change(comment="", log_action_comment="blocked Bob for 31h")
    assert not fast_reject(change, [filt])


def test_fast_reject_considers_union_of_all_filters():
    a = make_filter(userIncludeList=["Zed"])
    b = make_filter(pageIncludePatterns=["Sandbox"])
    # Neither user nor page matches filter a, but page matches filter b:
    # the global short-circuit must let it through.
    assert not fast_reject(make_change(), [a, b])


# ── match_filters ─────────────────────────────────────────────────────────── #

def test_match_filters_returns_key_and_link_style():
    customs = [
        (111, make_filter(userIncludeList=["Alice"], linkStyle="action")),
        (222, make_filter(userIncludeList=["Bob"])),
    ]
    assert match_filters(make_change(), customs, "thread") == [(111, "action")]


def test_match_filters_exclude_beats_include():
    filt = make_filter(userIncludeList=["Alice"], pageExcludePatterns=["Sandbox"])
    assert match_filters(make_change(), [(1, filt)], "thread") == []


def test_match_filters_broken_filter_skipped_others_still_match():
    class ExplodingFilter:
        link_style = "title"

        def matches(self, change):
            raise RuntimeError("boom")

    keyed = [
        ("bad", ExplodingFilter()),
        ("good", make_filter(userIncludeList=["Alice"])),
    ]
    assert match_filters(make_change(), keyed, "thread") == [("good", "title")]


def test_match_filters_site_name_restricts_server():
    filt = make_filter(userIncludeList=["Alice"], siteName="de.wikipedia.org")
    assert match_filters(make_change(), [(1, filt)], "thread") == []
    assert match_filters(
        make_change(server_name="de.wikipedia.org"), [(1, filt)], "thread"
    ) == [(1, "title")]


# ── truncate_for_discord ──────────────────────────────────────────────────── #

def test_truncate_noop_below_limit():
    msg = "x" * DISCORD_MESSAGE_LIMIT
    assert truncate_for_discord(msg) == msg


def test_truncate_clamps_and_marks():
    msg = "x" * (DISCORD_MESSAGE_LIMIT + 500)
    out = truncate_for_discord(msg)
    assert len(out) <= DISCORD_MESSAGE_LIMIT
    assert out.endswith(TRUNCATED_MESSAGE_SUFFIX)


# ── _event_ts_to_epoch (staleness input) ──────────────────────────────────── #

def test_ts_integer_epoch():
    assert _event_ts_to_epoch({"timestamp": 1700000000}) == 1700000000.0


def test_ts_numeric_string_epoch():
    assert _event_ts_to_epoch({"timestamp": "1700000000"}) == 1700000000.0


def test_ts_meta_dt_fallback():
    dt = datetime(2026, 1, 1, tzinfo=UTC)
    change = {"meta": {"dt": "2026-01-01T00:00:00Z"}}
    assert _event_ts_to_epoch(change) == dt.timestamp()


def test_ts_unparseable_returns_none():
    assert _event_ts_to_epoch({}) is None
    assert _event_ts_to_epoch({"timestamp": "not-a-number"}) is None
    assert _event_ts_to_epoch({"meta": {"dt": "garbage"}}) is None
