"""Redis persistence must preserve state and fail closed on unavailable storage."""
import asyncio
import copy
import json
import threading
from unittest.mock import AsyncMock

import pytest
import requests
from requests.adapters import BaseAdapter

import src.config as config


INITIAL = {"version": "0.9", "threads": {"123": {
    "name": "A thread", "active": True, "config": {"siteName": "en.wikipedia.org"},
}}, "receivers": {}}


class RedisHTTP(BaseAdapter):
    """Model only the external GET/SET API; use real request preparation/parsing."""
    def __init__(self):
        self.value = json.dumps(INITIAL)
        self.commands = []
        self.failure = None

    def send(self, request, **kwargs):
        assert request.headers["Authorization"] == "Bearer test-token"
        assert kwargs["timeout"] == (3, 5)
        command = json.loads(request.body)
        self.commands.append(command)
        assert command[1] == "fez:test:config"
        if self.failure:
            raise self.failure
        response = requests.Response()
        response.status_code = 200
        response.headers["Upstash-Sync-Token"] = "test-sync-token"
        if command[0] == "GET":
            assert len(command) == 2
            result = self.value
        else:
            assert command[0] == "SET"
            assert command[3:] in (["XX"], ["NX"])  # No TTL.
            eligible = self.value is not None if command[3] == "XX" else self.value is None
            result = "OK" if eligible else None
            if result:
                self.value = command[2]
        response._content = json.dumps({"result": result}).encode()
        return response

    def close(self):
        pass


@pytest.fixture
def redis_store(monkeypatch, tmp_path):
    from src.persistence import UpstashConfigStore
    remote = RedisHTTP()
    session = requests.Session()
    session.mount("https://test.upstash.io", remote)
    store = UpstashConfigStore("https://test.upstash.io", "test-token", "fez:test:config", session=session)
    monkeypatch.setattr(config, "REDIS_STORE", store)
    monkeypatch.setattr(config, "_CONFIG_DIRTY", False)
    monkeypatch.setattr(config, "CONFIG_LOCK", asyncio.Lock())
    monkeypatch.setattr(config, "STATE_FILE", tmp_path / "must-not-exist" / "config.json")
    original = copy.deepcopy(config.CONFIG)
    config.CONFIG.clear()
    config.CONFIG.update(copy.deepcopy(INITIAL))
    yield store, remote
    config.CONFIG.clear()
    config.CONFIG.update(original)
    session.close()


def test_load_and_save_use_redis_without_creating_files(redis_store):
    store, remote = redis_store
    loaded = config.load_config()
    assert loaded["threads"]["123"]["name"] == "A thread"
    assert loaded["threads"]["123"]["config"]["linkStyle"] == "title"
    loaded["threads"]["123"]["name"] = "Renamed"
    assert config.save_config(loaded) is True
    assert config.load_config()["threads"]["123"]["name"] == "Renamed"
    assert not config.STATE_FILE.parent.exists()
    assert [c[0] for c in remote.commands] == ["GET", "SET", "GET"]


@pytest.mark.parametrize("payload", [None, "broken-json", "[]", '{"threads": []}', '{"threads":{"1":null}}'])
def test_missing_or_corrupt_redis_state_never_becomes_defaults(redis_store, payload):
    _, remote = redis_store
    remote.value = payload
    with pytest.raises(RuntimeError):
        config.load_config()
    assert all(c[0] == "GET" for c in remote.commands)
    assert not config.STATE_FILE.parent.exists()


def test_redis_read_failure_does_not_fall_back_to_stale_file(redis_store):
    _, remote = redis_store
    config.STATE_FILE.parent.mkdir()
    config.STATE_FILE.write_text(json.dumps(INITIAL))
    remote.failure = requests.Timeout("a secret-bearing transport error")
    with pytest.raises(RuntimeError) as exc:
        config.load_config()
    assert "secret-bearing" not in str(exc.value)
    assert all(c[0] == "GET" for c in remote.commands)


def test_write_failure_is_reported_without_credentials(redis_store, caplog):
    _, remote = redis_store
    remote.failure = requests.Timeout("secret-token-do-not-log")
    assert config.save_config(INITIAL) is False
    assert "secret-token-do-not-log" not in caplog.text
    assert json.loads(remote.value) == INITIAL


def test_deleted_key_is_not_silently_recreated(redis_store):
    _, remote = redis_store
    remote.value = None
    assert config.save_config(INITIAL) is False
    assert remote.value is None


async def test_failed_mutation_does_not_change_live_configuration(redis_store):
    _, remote = redis_store
    remote.failure = requests.Timeout("unavailable")
    before = copy.deepcopy(config.CONFIG)
    with pytest.raises(RuntimeError):
        await config.update_entity_config("threads", "123", {"siteName": "other.wiki"})
    assert config.CONFIG == before
    assert json.loads(remote.value) == INITIAL


async def test_save_yields_event_loop_and_serializes_mutations(redis_store, monkeypatch):
    _, remote = redis_store
    original_save = config.save_config
    entered = threading.Event()
    release = threading.Event()

    def slow_save(value):
        entered.set()
        assert release.wait(3)
        return original_save(value)

    monkeypatch.setattr(config, "save_config", slow_save)
    first = asyncio.create_task(config.set_custom_thread_active("123", False))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert config.CONFIG["threads"]["123"]["active"] is True
        second = asyncio.create_task(config.update_entity_config("threads", "123", {"siteName": "other.wiki"}))
        await asyncio.sleep(0)
        assert not second.done()
    finally:
        release.set()
    assert await first is True
    assert await second is True
    stored = json.loads(remote.value)
    assert stored["threads"]["123"]["active"] is False
    assert stored["threads"]["123"]["config"] == {"siteName": "other.wiki"}


async def test_global_import_reports_failure_instead_of_success(redis_store):
    _, remote = redis_store
    from src.commands.config_commands import globalconfig_setraw_cmd
    from src.main import on_command_error
    from discord.ext.commands import CommandInvokeError
    before = copy.deepcopy(config.CONFIG)
    remote.failure = requests.Timeout("unavailable")
    ctx = AsyncMock()
    attachment = AsyncMock()
    attachment.read.return_value = json.dumps({"version": "0.9", "threads": {}, "receivers": {}}).encode()
    with pytest.raises(RuntimeError) as exc:
        await globalconfig_setraw_cmd.callback(ctx, attachment)
    assert config.CONFIG == before
    ctx.reply.assert_not_called()
    await on_command_error(ctx, CommandInvokeError(exc.value))
    assert "could not confirm" in ctx.reply.call_args[0][0].lower()


@pytest.mark.parametrize("values", [
    {"FEZ_CONFIG_BACKEND": "typo"},
    {"FEZ_CONFIG_BACKEND": "redis"},
    {"FEZ_CONFIG_BACKEND": "redis", "UPSTASH_REDIS_REST_URL": "http://insecure", "UPSTASH_REDIS_REST_TOKEN": "test"},
])
def test_bad_backend_settings_fail_instead_of_using_disk(monkeypatch, values):
    from src.persistence import store_from_env
    for name in ("FEZ_CONFIG_BACKEND", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN", "FEZ_REDIS_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError):
        store_from_env()


async def test_automatic_disables_survive_storage_outage(redis_store, caplog):
    _, remote = redis_store
    config.CONFIG["receivers"]["broken"] = {"active": True, "config": {}}
    remote.failure = requests.Timeout("unavailable")
    assert await config.set_custom_thread_active("123", False, best_effort=True)
    assert await config.set_receiver_errored("broken")
    assert config.CONFIG["threads"]["123"]["active"] is False
    assert config.CONFIG["receivers"]["broken"]["active"] is False
    assert config.CONFIG["receivers"]["broken"]["errored"] is True
    assert "only in memory" in caplog.text
    remote.failure = None
    await config.update_entity_config("threads", "123", {"siteName": "other.wiki"})
    assert json.loads(remote.value) == config.CONFIG


async def test_cancelled_writer_retains_lock_until_save_finishes(redis_store, monkeypatch):
    _, remote = redis_store
    original_save = config.save_config
    entered, release = threading.Event(), threading.Event()

    def slow_save(value):
        entered.set()
        assert release.wait(3)
        return original_save(value)

    monkeypatch.setattr(config, "save_config", slow_save)
    first = asyncio.create_task(config.set_custom_thread_active("123", False))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        for _ in range(2):
            first.cancel()
            await asyncio.sleep(0)
        second = asyncio.create_task(config.update_entity_config("threads", "123", {"siteName": "other.wiki"}))
        await asyncio.sleep(0)
        assert config.CONFIG_LOCK.locked()
        assert not first.done()
        assert not second.done()
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
    assert first.cancelled()
    assert json.loads(remote.value)["threads"]["123"]["active"] is False
    assert json.loads(remote.value)["threads"]["123"]["config"]["siteName"] == "other.wiki"


def test_import_creates_missing_state_and_is_idempotent(redis_store):
    from src.migrate_config import import_config
    store, remote = redis_store
    remote.value = None
    import_config(store, INITIAL)
    assert json.loads(remote.value) == INITIAL
    import_config(store, INITIAL)
    assert json.loads(remote.value) == INITIAL


def test_import_never_overwrites_different_existing_state(redis_store):
    from src.migrate_config import import_config
    store, remote = redis_store
    with pytest.raises(RuntimeError, match="different"):
        import_config(store, {"threads": {}, "receivers": {}})
    assert json.loads(remote.value) == INITIAL


def test_export_preserves_configuration_without_overwriting_local_file(redis_store, tmp_path):
    from src.migrate_config import export_config
    store, remote = redis_store
    path = tmp_path / "backup.json"
    export_config(store, path)
    assert json.loads(path.read_text()) == INITIAL
    assert path.stat().st_mode & 0o777 == 0o600
    remote.value = json.dumps({"threads": {}})
    with pytest.raises(FileExistsError):
        export_config(store, path)
    assert json.loads(path.read_text()) == INITIAL


@pytest.fixture
def live_routing(redis_store, monkeypatch):
    import src.event_streams as streams
    from src.routing import RoutingPlanCache
    monkeypatch.setattr(streams, "CONFIG", config.CONFIG)
    monkeypatch.setattr(streams, "ROUTING_CACHE", RoutingPlanCache())
    monkeypatch.setattr(streams, "WEBHOOKS", {"healthy": "test-webhook"})
    return streams


def routing_event(user="Alice"):
    import time
    return {"timestamp": time.time(), "user": user, "title": "Article", "type": "new", "server_name": "en.wikipedia.org"}


async def test_routing_tracks_confirmed_redis_edits_and_raw_replacement(live_routing):
    await config.set_entity_config_field("threads", "123", "userIncludeList", ["Alice"])
    assert (await live_routing.get_routing_plan()).route(routing_event()) == [("thread", 123, "title")]
    await config.set_entity_config_field("threads", "123", "userIncludeList", ["Bob"])
    assert (await live_routing.get_routing_plan()).route(routing_event()) == []
    assert (await live_routing.get_routing_plan()).route(routing_event("Bob")) == [("thread", 123, "title")]
    await config.replace_config({"threads": {}, "receivers": {}})
    assert (await live_routing.get_routing_plan()).route(routing_event("Bob")) == []


async def test_rejected_redis_edit_keeps_the_published_routing_plan(live_routing, redis_store):
    _, remote = redis_store
    await config.set_entity_config_field("threads", "123", "userIncludeList", ["Alice"])
    plan = await live_routing.get_routing_plan()
    remote.failure = requests.Timeout("unavailable")
    with pytest.raises(RuntimeError):
        await config.set_entity_config_field("threads", "123", "userIncludeList", ["Bob"])
    assert await live_routing.get_routing_plan() is plan
    assert plan.route(routing_event()) == [("thread", 123, "title")]
    assert plan.route(routing_event("Bob")) == []


@pytest.mark.parametrize("kind", ["thread", "receiver"])
async def test_routing_refreshes_for_automatic_disables_during_redis_outage(live_routing, redis_store, kind):
    _, remote = redis_store
    proposed = copy.deepcopy(INITIAL)
    proposed["threads"]["123"]["config"]["userIncludeList"] = ["Alice"]
    proposed["receivers"]["healthy"] = {"active": True, "config": {"userIncludeList": ["Alice"]}}
    await config.replace_config(proposed)
    assert len((await live_routing.get_routing_plan()).route(routing_event())) == 2
    remote.failure = requests.Timeout("unavailable")
    if kind == "thread":
        await config.set_custom_thread_active("123", False, best_effort=True)
        expected = [("receiver", "healthy", "title")]
    else:
        await config.set_receiver_errored("healthy")
        expected = [("thread", 123, "title")]
    assert (await live_routing.get_routing_plan()).route(routing_event()) == expected


@pytest.mark.parametrize("cancel_edit", [False, True])
async def test_slow_redis_save_does_not_block_routing_or_delivery(live_routing, redis_store, monkeypatch, cancel_edit):
    from types import SimpleNamespace
    streams = live_routing
    _, remote = redis_store
    await config.replace_config({"threads": {}, "receivers": {
        "healthy": {"active": True, "config": {"userIncludeList": ["Alice"]}},
    }})
    entered, release = threading.Event(), threading.Event()
    original_send = remote.send
    def slow_send(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original_send(*args, **kwargs)
    monkeypatch.setattr(remote, "send", slow_send)
    incoming = asyncio.Queue()
    queue_type = asyncio.Queue
    monkeypatch.setattr(streams, "asyncio", SimpleNamespace(**dict(vars(asyncio), Queue=lambda maxsize=0: incoming if maxsize == streams.EventStreamConfig.QUEUE_MAX_SIZE else queue_type(maxsize))))
    monkeypatch.setattr(streams, "threading", SimpleNamespace(Thread=lambda **kwargs: SimpleNamespace(start=lambda: None)))
    delivered = asyncio.Event()
    async def send(url, content, key):
        assert key == "healthy"
        delivered.set()
        return True
    monkeypatch.setattr(streams, "send_webhook_with_backoff", send)
    edit = asyncio.create_task(config.set_entity_config_field("receivers", "healthy", "userIncludeList", ["Bob"]))
    worker = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        incoming.put_nowait(routing_event())
        worker = asyncio.create_task(streams.stream_worker(None))
        await asyncio.wait_for(delivered.wait(), 1)
        assert not edit.done()
        if cancel_edit:
            edit.cancel()
    finally:
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        release.set()
        if cancel_edit and edit.cancelling():
            with pytest.raises(asyncio.CancelledError):
                await edit
        else:
            await edit
    assert (await streams.get_routing_plan()).route(routing_event()) == []
    assert (await streams.get_routing_plan()).route(routing_event("Bob")) == [("receiver", "healthy", "title")]
