import json

from discord import Attachment
from discord.ext import commands

from src.bot_instance import bot
from src.commands.helpers import (
    _deepcopy_cfg,
    _parse_json_arg,
    _require_custom_thread,
    authorised,
    read_json_attachment,
    require_list_key,
    send_json_file,
    validate_config_value,
)
from src.config import (
    CONFIG,
    CONFIG_LOCK,
    _normalise_list_val,
    mutate_custom_thread_config_list,
    save_config,
    update_custom_thread_config,
)
from src.constants import JSON_INDENT
from src.logging_setup import logger

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
        snapshot = _deepcopy_cfg(CONFIG)
    await send_json_file(ctx, snapshot, "global_config.json")


@globalconfig_group.command(name="setraw",
                            description="Replace configuration from attached JSON (DANGEROUS)")
@commands.check(authorised)
async def globalconfig_setraw_cmd(ctx: commands.Context, attachment: Attachment = None):
    logger.warning(f"Global config setraw command from {ctx.author} - DANGEROUS OPERATION")
    new_cfg = await read_json_attachment(ctx, attachment, "/globalconfig setraw")
    if new_cfg is None:
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
    logger.info(f"Config get command from {ctx.author} in thread {ctx.channel.id}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    pretty = json.dumps(entry["config"], indent=JSON_INDENT)
    await ctx.reply(f"```json\n{pretty}\n```")


@config_group.command(name="set",
                      description="Set a configuration key")
async def config_set_cmd(ctx: commands.Context, key: str, *, value: str):
    logger.info(f"Config set command from {ctx.author} in thread {ctx.channel.id}: {key} = {value}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    err, parsed = validate_config_value(key, _parse_json_arg(value))
    if err:
        await ctx.reply(err)
        return
    cfg = _deepcopy_cfg(entry["config"])
    cfg[key] = parsed
    ok = await update_custom_thread_config(str(ctx.channel.id), cfg)
    if ok:
        await ctx.reply(f"Set `{key}`: `{parsed}`")
    else:
        logger.error(f"Failed to set config {key} for thread {ctx.channel.id}")
        await ctx.reply("Failed to set.")


async def _mutate_thread_list(ctx: commands.Context, key: str, value: str, *, op: str) -> None:
    """Shared body of /config add|remove: validate, mutate, report."""
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    err = require_list_key(key)
    if err:
        await ctx.reply(err)
        return
    vals = _normalise_list_val(_parse_json_arg(value))
    kwargs = {"add": vals} if op == "add" else {"remove": vals}
    ok, new_list = await mutate_custom_thread_config_list(str(ctx.channel.id), key, **kwargs)
    if ok:
        verb = "Added to" if op == "add" else "Removed from"
        await ctx.reply(f"{verb} `{key}`: `{vals}`\nNow: `{new_list}`")
    else:
        logger.error(f"Failed to {op} on config {key} for thread {ctx.channel.id}")
        await ctx.reply(f"Failed to {op}.")


@config_group.command(name="add",
                      description="Append value(s) to a list key")
async def config_add_cmd(ctx: commands.Context, key: str, *, value: str):
    logger.info(f"Config add command from {ctx.author} in thread {ctx.channel.id}: {key} += {value}")
    await _mutate_thread_list(ctx, key, value, op="add")


@config_group.command(name="remove",
                      description="Remove value(s) from a list key")
async def config_remove_cmd(ctx: commands.Context, key: str, *, value: str):
    logger.info(f"Config remove command from {ctx.author} in thread {ctx.channel.id}: {key} -= {value}")
    await _mutate_thread_list(ctx, key, value, op="remove")


@config_group.command(name="clear",
                      description="Clear a list key")
async def config_clear_cmd(ctx: commands.Context, key: str):
    logger.info(f"Config clear command from {ctx.author} in thread {ctx.channel.id}: clear {key}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    err = require_list_key(key)
    if err:
        await ctx.reply(err)
        return
    ok, _ = await mutate_custom_thread_config_list(str(ctx.channel.id), key, clear=True)
    if ok:
        await ctx.reply(f"Cleared `{key}`.")
    else:
        logger.error(f"Failed to clear config {key} for thread {ctx.channel.id}")
        await ctx.reply("Failed to clear.")


@config_group.command(name="getraw",
                      description="Download raw JSON configuration for this thread")
@commands.check(authorised)
async def config_getraw_cmd(ctx: commands.Context):
    logger.info(f"Config getraw command from {ctx.author} in thread {ctx.channel.id}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    await send_json_file(ctx, entry["config"], f"thread_{ctx.channel.id}_config.json")


@config_group.command(name="setraw",
                      description="Replace this thread's configuration from attached JSON (DANGEROUS)")
@commands.check(authorised)
async def config_setraw_cmd(ctx: commands.Context, attachment: Attachment = None):
    logger.warning(f"Config setraw command from {ctx.author} in thread {ctx.channel.id}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    new_cfg = await read_json_attachment(ctx, attachment, "/config setraw")
    if new_cfg is None:
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
    logger.info(f"Status command from {ctx.author} in thread {ctx.channel.id}")
    await ctx.invoke(config_get_cmd)
