"""End-to-end tests for the event pipeline consumer (process_event).

These exercise the real routing path — config snapshot → filter compile/cache
→ fast-reject → match → dispatch — with only the Discord senders faked.
Before the Phase 4 decomposition this logic was inline in stream_worker's
loop and untestable.
"""

import time

import pytest

import src.config as config_mod
import src.event_streams as es
from src.config import CONFIG, WEBHOOKS
from src.models import FilterConfig
from tests.factories import make_change as base_change

WEBHOOK_URL = "https://discord.com/api/webhooks/1/token"


class FakeChannel:
    """Stands in for the parent TextChannel in operator announcements."""

    def __init__(self):
        self.name = "parent"


@pytest.fixture(autouse=True)
def pipeline_env(tmp_path, monkeypatch):
    """Isolated CONFIG/WEBHOOKS/caches + captured sends for every test."""
    monkeypatch.setattr(config_mod, "STATE_FILE", tmp_path / "config.json")

    CONFIG.clear()
    CONFIG.update({"version": "0.9", "threads": {}, "receivers": {}})
    WEBHOOKS.clear()

    es.THREAD_FILTERS.clear()
    es.RECEIVER_FILTERS.clear()
    es.THREAD_CACHE.clear()

    sent = {"messages": [], "webhooks": []}

    async def fake_send_message(target, content):
        sent["messages"].append((target, content))
        return object()  # a "Message"

    async def fake_send_webhook(url, content, key):
        sent["webhooks"].append((url, content, key))
        return True

    monkeypatch.setattr(es, "send_message_with_backoff", fake_send_message)
    monkeypatch.setattr(es, "send_webhook_with_backoff", fake_send_webhook)

    yield sent


def add_thread(tid: int, *, active=True, **filter_overrides):
    cfg = FilterConfig(**filter_overrides).to_dict()
    CONFIG["threads"][str(tid)] = {"name": f"t{tid}", "active": active, "config": cfg}


def add_receiver(key: str, *, active=True, errored=False, with_webhook=True, **filter_overrides):
    cfg = FilterConfig(**filter_overrides).to_dict()
    CONFIG["receivers"][key] = {
        "name": key, "active": active, "errored": errored, "config": cfg,
    }
    if with_webhook:
        WEBHOOKS[key] = WEBHOOK_URL


def make_change(**overrides) -> dict:
    change = base_change(**overrides)
    change.setdefault("timestamp", int(time.time()))
    return change


async def test_matching_event_reaches_thread_and_receiver(pipeline_env):
    add_thread(111, user_include_list=["Alice"])
    add_receiver("hook", user_include_list=["Alice"])
    fake_thread = object()
    es.THREAD_CACHE[111] = fake_thread

    await es.process_event(make_change(), FakeChannel())

    assert [t for t, _ in pipeline_env["messages"]] == [fake_thread]
    assert "**Alice** edited" in pipeline_env["messages"][0][1]
    assert [(u, k) for u, _, k in pipeline_env["webhooks"]] == [(WEBHOOK_URL, "hook")]


async def test_non_matching_event_sends_nothing(pipeline_env):
    add_thread(111, user_include_list=["Bob"])
    es.THREAD_CACHE[111] = object()

    await es.process_event(make_change(), FakeChannel())

    assert pipeline_env["messages"] == []
    assert pipeline_env["webhooks"] == []


async def test_stale_event_dropped(pipeline_env):
    add_thread(111, user_include_list=["Alice"])
    es.THREAD_CACHE[111] = object()

    stale_ts = int(time.time()) - (config_mod.STALENESS_SECS + 60)
    await es.process_event(make_change(timestamp=stale_ts), FakeChannel())

    assert pipeline_env["messages"] == []


async def test_userless_event_dropped(pipeline_env):
    add_thread(111, user_include_list=["Alice"])
    es.THREAD_CACHE[111] = object()

    await es.process_event(make_change(user=None), FakeChannel())

    assert pipeline_env["messages"] == []


async def test_inactive_thread_and_errored_receiver_skipped(pipeline_env):
    add_thread(111, active=False, user_include_list=["Alice"])
    add_receiver("dead", errored=True, user_include_list=["Alice"])
    es.THREAD_CACHE[111] = object()

    await es.process_event(make_change(), FakeChannel())

    assert pipeline_env["messages"] == []
    assert pipeline_env["webhooks"] == []


async def test_failed_thread_send_evicts_cache(pipeline_env, monkeypatch):
    add_thread(111, user_include_list=["Alice"])
    es.THREAD_CACHE[111] = object()

    async def failing_send(target, content):
        return None  # what send_message_with_backoff returns on failure

    monkeypatch.setattr(es, "send_message_with_backoff", failing_send)

    await es.process_event(make_change(), FakeChannel())

    assert 111 not in es.THREAD_CACHE  # next event re-fetches & re-classifies


async def test_permanent_webhook_failure_disables_receiver_and_announces(pipeline_env, monkeypatch):
    add_receiver("hook", user_include_list=["Alice"])
    channel = FakeChannel()

    async def broken_webhook(url, content, key):
        from src.discord_utils import WebhookError
        raise WebhookError("Webhook not found (404)")

    monkeypatch.setattr(es, "send_webhook_with_backoff", broken_webhook)

    await es.process_event(make_change(), channel)

    entry = CONFIG["receivers"]["hook"]
    assert entry["errored"] is True
    assert entry["active"] is False
    # The disable notice went to the parent channel via the (faked) sender.
    notices = [c for t, c in pipeline_env["messages"] if t is channel]
    assert any("hook" in n and "disabled" in n for n in notices)


async def test_transient_webhook_failure_keeps_receiver_active(pipeline_env, monkeypatch):
    add_receiver("hook", user_include_list=["Alice"])

    async def flaky_webhook(url, content, key):
        from src.discord_utils import TransientWebhookError
        raise TransientWebhookError("HTTP 503")

    monkeypatch.setattr(es, "send_webhook_with_backoff", flaky_webhook)

    await es.process_event(make_change(), FakeChannel())

    entry = CONFIG["receivers"]["hook"]
    assert entry["errored"] is False
    assert entry["active"] is True


async def test_invalid_persisted_regex_skips_target_not_pipeline(pipeline_env):
    # A bad regex can reach the config via setraw (bypassing command
    # validation). It must not crash the pipeline into a restart loop —
    # the broken target is skipped, everything else keeps routing.
    add_thread(111, user_include_list=["Alice"])
    CONFIG["threads"]["111"]["config"]["pageIncludePatterns"] = ["("]
    add_thread(222, user_include_list=["Alice"])
    good_thread = object()
    es.THREAD_CACHE[222] = good_thread

    await es.process_event(make_change(), FakeChannel())  # must not raise

    assert [t for t, _ in pipeline_env["messages"]] == [good_thread]


async def test_filter_cache_rebuilds_on_config_change(pipeline_env):
    add_thread(111, user_include_list=["Alice"])
    es.THREAD_CACHE[111] = object()

    await es.process_event(make_change(), FakeChannel())
    assert len(pipeline_env["messages"]) == 1

    # Change the filter config: Alice no longer matches.
    CONFIG["threads"]["111"]["config"] = FilterConfig(user_include_list=["Bob"]).to_dict()

    await es.process_event(make_change(), FakeChannel())
    assert len(pipeline_env["messages"]) == 1  # unchanged — stale filter was rebuilt
