"""Exercise persistence through command callbacks and real hybrid dispatch."""
import asyncio
import copy
import json
import threading
from types import SimpleNamespace, MethodType
from unittest.mock import AsyncMock, MagicMock

import discord
from discord.ext import commands
import pytest
import requests

from test_redis_config import redis_store
import src.config as config
from src.bot_instance import bot
from src.commands.config_commands import config_set_cmd, globalconfig_getraw_cmd, globalconfig_setraw_cmd
from src.commands.receiver_commands import receiver_setconfig_cmd, receiver_deactivate_cmd
from src.commands.thread_commands import deactivate_custom_thread_cmd


def thread_context():
    thread = MagicMock(spec=discord.Thread)
    thread.id, thread.name, thread.parent_id = 123, "A thread", config.DISCORD_CHANNEL_ID
    return SimpleNamespace(channel=thread, author=SimpleNamespace(id=config.OWNER_ID),
                           reply=AsyncMock(), interaction=None)


@pytest.mark.parametrize("entity_type", ["threads", "receivers"])
async def test_queued_field_commands_preserve_both_changes(redis_store, monkeypatch, entity_type):
    _, remote = redis_store
    config.CONFIG["receivers"]["123"] = copy.deepcopy(config.CONFIG["threads"]["123"])
    original_save = config.save_config
    entered, release = threading.Event(), threading.Event()

    def delayed_save(value):
        entered.set()
        assert release.wait(5)
        return original_save(value)

    monkeypatch.setattr(config, "save_config", delayed_save)
    pending = [asyncio.create_task(config.set_custom_thread_active("123", False))]
    a, b = thread_context(), thread_context()
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        command = config_set_cmd if entity_type == "threads" else receiver_setconfig_cmd
        prefix = [] if entity_type == "threads" else ["123"]
        pending.append(asyncio.create_task(command.callback(a, *prefix, "userIncludeList", value='["Alice"]')))
        pending.append(asyncio.create_task(command.callback(b, *prefix, "pageIncludePatterns", value='["Example"]')))
        await asyncio.sleep(0)
    finally:
        release.set()
        await asyncio.gather(*pending)
    actual = json.loads(remote.value)[entity_type]["123"]["config"]
    assert actual.get("userIncludeList") == ["Alice"]
    assert actual.get("pageIncludePatterns") == ["Example"]


@pytest.mark.parametrize("entity_type", ["threads", "receivers"])
async def test_repeated_disable_persists_after_storage_recovers(redis_store, entity_type):
    _, remote = redis_store
    config.CONFIG["receivers"]["123"] = copy.deepcopy(config.CONFIG["threads"]["123"])
    assert config.save_config(config.CONFIG)
    remote.failure = requests.Timeout("simulated outage")
    if entity_type == "threads":
        await config.set_custom_thread_active("123", False, best_effort=True)
    else:
        await config.set_receiver_errored("123")
    assert config.CONFIG[entity_type]["123"]["active"] is False
    remote.failure = None
    if entity_type == "threads":
        await deactivate_custom_thread_cmd.callback(thread_context())
    else:
        await receiver_deactivate_cmd.callback(thread_context(), "123")
    assert json.loads(remote.value)[entity_type]["123"]["active"] is False
    # A subsequent confirmed no-op should not spend another Redis write.
    remote.commands.clear()
    if entity_type == "threads":
        await config.set_custom_thread_active("123", False)
    else:
        await config.set_receiver_active("123", False)
    assert remote.commands == []


class InteractionResponse:
    """Discord boundary: acknowledge once, before any follow-up is sent."""
    def __init__(self):
        self.acknowledged = False

    def is_done(self):
        return self.acknowledged

    async def defer(self, **kwargs):
        assert not self.acknowledged
        self.acknowledged = True


def slash_dispatch(monkeypatch, command, kwargs):
    from src.main import on_command_error
    ctx = thread_context()
    ctx.bot, ctx.command_failed = bot, False
    ctx.message = SimpleNamespace(content="")
    interaction = SimpleNamespace(client=bot, namespace=SimpleNamespace(**kwargs),
                                  response=InteractionResponse())
    ctx.interaction = interaction
    ctx.defer = MethodType(commands.Context.defer, ctx)
    monkeypatch.setattr(bot, "get_context", AsyncMock(return_value=ctx))
    monkeypatch.setattr(bot, "dispatch", lambda *args: None)

    async def dispatch_error(context, error):
        context.command_failed = True
        await on_command_error(context, error)

    monkeypatch.setattr(command, "dispatch_error", dispatch_error)
    return ctx, command.app_command._invoke_with_namespace(interaction, interaction.namespace)


async def test_slash_storage_error_reaches_operator(redis_store, monkeypatch):
    _, remote = redis_store
    remote.failure = requests.Timeout("simulated outage")
    attachment = AsyncMock(spec=discord.Attachment)
    attachment.read.return_value = b'{"threads": {}, "receivers": {}}'
    ctx, invocation = slash_dispatch(monkeypatch, globalconfig_setraw_cmd, {"attachment": attachment})
    await invocation
    assert ctx.command_failed
    assert "could not confirm" in ctx.reply.call_args.args[0].lower()
    assert json.loads(remote.value)["threads"]["123"]["active"] is True


@pytest.mark.parametrize("command,kwargs", [
    (config_set_cmd, {"key": "siteName", "value": "other.wiki"}),
    (globalconfig_getraw_cmd, {}),
    (receiver_deactivate_cmd, {"key": "123"}),
])
async def test_slash_acknowledges_before_waiting_for_storage_lock(redis_store, monkeypatch, command, kwargs):
    monkeypatch.setattr("src.commands.config_commands.CONFIG_LOCK", config.CONFIG_LOCK)
    config.CONFIG["receivers"]["123"] = copy.deepcopy(config.CONFIG["threads"]["123"])
    ctx, invocation = slash_dispatch(monkeypatch, command, kwargs)
    await config.CONFIG_LOCK.acquire()
    pending = asyncio.create_task(invocation)
    try:
        await asyncio.sleep(0)
        assert not pending.done()
        assert ctx.interaction.response.is_done()
    finally:
        config.CONFIG_LOCK.release()
        await pending
    assert not ctx.command_failed
    assert ctx.reply.call_count == 1


async def test_slow_import_defers_before_attachment_or_save(redis_store, monkeypatch):
    original_save = config.save_config
    entered, release = threading.Event(), threading.Event()
    attachment = AsyncMock(spec=discord.Attachment)
    ctx, invocation = slash_dispatch(monkeypatch, globalconfig_setraw_cmd, {"attachment": attachment})
    acknowledged_at_read = []

    async def read():
        acknowledged_at_read.append(ctx.interaction.response.is_done())
        return b'{"threads": {}, "receivers": {}}'

    def delayed_save(value):
        entered.set()
        assert release.wait(5)
        return original_save(value)

    attachment.read.side_effect = read
    monkeypatch.setattr(config, "save_config", delayed_save)
    pending = asyncio.create_task(invocation)
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert acknowledged_at_read == [True]
        assert ctx.interaction.response.is_done()
        assert not pending.done()
        assert ctx.reply.call_count == 0
    finally:
        release.set()
        await pending
    assert not ctx.command_failed
    assert "replaced" in ctx.reply.call_args.args[0].lower()
