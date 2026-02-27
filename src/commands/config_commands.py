import json
import io

import discord
from discord.ext import commands

from src.logging_setup import logger
from src.bot_instance import bot
from src.constants import JSON_INDENT, UTF8_ENCODING
from src.config import (
    CONFIG,
    CONFIG_LOCK,
    save_config,
    update_custom_thread_config,
    mutate_custom_thread_config_list,
    _normalise_list_val,
)
from src.commands.helpers import (
    authorised,
    _require_custom_thread,
    _parse_json_arg,
    _deepcopy_cfg,
)


# ── /globalconfig ─────────────────────────────────────────────────────────── #

@bot.hybrid_group(name="globalconfig",
                  invoke_without_command=True,
                  description="Global configuration operations",
                  with_app_command=True)
@commands.check(authorised)
async def globalconfig_group(ctx: commands.Context):
    await ctx.invoke(globalconfig_getraw_cmd)


@globalconfig_group.command(name="getraw",
                            description="Download full configuration as JSON")
@commands.check(authorised)
async def globalconfig_getraw_cmd(ctx: commands.Context):
    logger.info(f"Global config getraw command from {ctx.author}")
    async with CONFIG_LOCK:
        data = json.dumps(CONFIG, indent=JSON_INDENT).encode(UTF8_ENCODING)
    file = discord.File(io.BytesIO(data), filename="global_config.json")
    await ctx.reply(file=file, mention_author=False)


@globalconfig_group.command(name="setraw",
                            description="Replace configuration from attached JSON (DANGEROUS)")
@commands.check(authorised)
async def globalconfig_setraw_cmd(ctx: commands.Context, attachment: discord.Attachment = None):
    logger.warning(f"Global config setraw command from {ctx.author} - DANGEROUS OPERATION")
    if attachment is None:
        if ctx.message is None or not ctx.message.attachments:
            await ctx.reply("Attach a **JSON** file to `/globalconfig setraw`.", mention_author=False)
            return
        attachment = ctx.message.attachments[0]
    try:
        raw = await attachment.read()
        new_cfg = json.loads(raw)
    except Exception as e:
        await ctx.reply(f"Could not parse attachment as JSON: {e}", mention_author=False)
        return
    async with CONFIG_LOCK:
        CONFIG.clear()
        CONFIG.update(new_cfg)
        save_config(CONFIG)
    await ctx.reply("Global configuration **replaced**.", mention_author=False)


# ── /config ───────────────────────────────────────────────────────────────── #

@bot.hybrid_group(name="config",
                  invoke_without_command=True,
                  description="Thread configuration operations",
                  with_app_command=True)
async def config_group(ctx: commands.Context):
    await ctx.invoke(config_get_cmd)


@config_group.command(name="get",
                      description="Show current thread configuration")
async def config_get_cmd(ctx: commands.Context):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    pretty = json.dumps(entry["config"], indent=JSON_INDENT)
    await ctx.reply(f"```json\n{pretty}\n```")


@config_group.command(name="set",
                      description="Set a configuration key")
async def config_set_cmd(ctx: commands.Context, key: str, *, value: str):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    parsed = _parse_json_arg(value)
    cfg = _deepcopy_cfg(entry["config"])
    cfg[key] = parsed
    ok = await update_custom_thread_config(str(ctx.channel.id), cfg)
    if ok:
        await ctx.reply(f"Set `{key}`: `{parsed}`")
    else:
        await ctx.reply("Failed to set.")


@config_group.command(name="add",
                      description="Append value(s) to a list key")
async def config_add_cmd(ctx: commands.Context, key: str, *, value: str):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    vals = _normalise_list_val(_parse_json_arg(value))
    ok, new_list = await mutate_custom_thread_config_list(str(ctx.channel.id), key, add=vals)
    if ok:
        await ctx.reply(f"Added to `{key}`: `{vals}`\nNow: `{new_list}`")
    else:
        await ctx.reply("Failed to add.")


@config_group.command(name="remove",
                      description="Remove value(s) from a list key")
async def config_remove_cmd(ctx: commands.Context, key: str, *, value: str):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    vals = _normalise_list_val(_parse_json_arg(value))
    ok, new_list = await mutate_custom_thread_config_list(str(ctx.channel.id), key, remove=vals)
    if ok:
        await ctx.reply(f"Removed from `{key}`: `{vals}`\nNow: `{new_list}`")
    else:
        await ctx.reply("Failed to remove.")


@config_group.command(name="clear",
                      description="Clear a list key")
async def config_clear_cmd(ctx: commands.Context, key: str):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    ok, _ = await mutate_custom_thread_config_list(str(ctx.channel.id), key, clear=True)
    if ok:
        await ctx.reply(f"Cleared `{key}`.")
    else:
        await ctx.reply("Failed to clear.")


@config_group.command(name="getraw",
                      description="Download raw JSON configuration for this thread")
@commands.check(authorised)
async def config_getraw_cmd(ctx: commands.Context):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    data = json.dumps(entry["config"], indent=JSON_INDENT).encode(UTF8_ENCODING)
    file = discord.File(io.BytesIO(data),
                        filename=f"thread_{ctx.channel.id}_config.json")
    await ctx.reply(file=file, mention_author=False)


@config_group.command(name="setraw",
                      description="Replace this thread's configuration from attached JSON (DANGEROUS)")
@commands.check(authorised)
async def config_setraw_cmd(ctx: commands.Context, attachment: discord.Attachment = None):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    if attachment is None:
        if ctx.message is None or not ctx.message.attachments:
            await ctx.reply("Attach a **JSON** file to `/config setraw`.", mention_author=False)
            return
        attachment = ctx.message.attachments[0]
    try:
        raw = await attachment.read()
        new_cfg = json.loads(raw)
    except Exception as e:
        await ctx.reply(f"Could not parse attachment as JSON: {e}", mention_author=False)
        return
    ok = await update_custom_thread_config(str(ctx.channel.id), new_cfg)
    if ok:
        await ctx.reply("Thread configuration **replaced**.", mention_author=False)
    else:
        await ctx.reply("Failed to replace configuration.", mention_author=False)


# ── /status (alias for /config get) ──────────────────────────────────────── #

@bot.hybrid_command(name="status",
                    description="Show current thread configuration",
                    with_app_command=True)
async def status_cmd(ctx: commands.Context):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    pretty = json.dumps(entry["config"], indent=JSON_INDENT)
    await ctx.reply(f"```json\n{pretty}\n```")
