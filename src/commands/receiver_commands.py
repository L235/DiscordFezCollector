import json

import discord
from discord.ext import commands

from src.logging_setup import logger
from src.bot_instance import bot
from src.constants import JSON_INDENT
from src.config import (
    CONFIG,
    CONFIG_LOCK,
    WEBHOOKS,
    create_receiver,
    delete_receiver,
    set_receiver_active,
    get_receiver_entry,
    update_receiver_config,
)
from src.commands.helpers import (
    is_bot_owner,
    FilterType,
    _parse_json_arg,
    _deepcopy_cfg,
    mutate_filter,
)


@bot.hybrid_group(name="receiver",
                  invoke_without_command=True,
                  description="Manage webhook receivers (owner only)",
                  with_app_command=True)
@commands.check(is_bot_owner)
async def receiver_group(ctx: commands.Context):
    await ctx.reply(
        "**Receiver Commands (owner only):**\n"
        "* `/receiver list` - List all receivers\n"
        "* `/receiver add <key> <name>` - Create a new receiver\n"
        "* `/receiver remove <key>` - Delete a receiver\n"
        "* `/receiver activate <key>` / `/receiver deactivate <key>` - Toggle receiver\n"
        "* `/receiver config <key>` - Show receiver config\n"
        "* `/receiver track|ignore|untrack|unignore <key> page|user|summary <value>` - Manage filters"
    )


@receiver_group.command(name="list", description="List all receivers")
@commands.check(is_bot_owner)
async def receiver_list_cmd(ctx: commands.Context):
    logger.info(f"Receiver list command from {ctx.author}")
    async with CONFIG_LOCK:
        receivers = CONFIG.get("receivers", {})
    if not receivers:
        await ctx.reply("No receivers configured.")
        return
    lines = ["**Configured Receivers:**"]
    for key, entry in receivers.items():
        status_parts = []
        status_parts.append("active" if entry.get("active") else "inactive")
        if entry.get("errored"):
            status_parts.append("ERRORED")
        has_webhook = "webhook" if key in WEBHOOKS else "NO WEBHOOK"
        status = ", ".join(status_parts)
        lines.append(f"* `{key}`: {entry.get('name', '(unnamed)')} [{status}, {has_webhook}]")
    await ctx.reply("\n".join(lines))


@receiver_group.command(name="add", description="Create a new receiver")
@commands.check(is_bot_owner)
async def receiver_add_cmd(ctx: commands.Context, key: str, *, name: str):
    logger.info(f"Receiver add command from {ctx.author}: key={key}, name={name}")
    ok, msg = await create_receiver(key, name)
    await ctx.reply(msg)


@receiver_group.command(name="remove", description="Delete a receiver")
@commands.check(is_bot_owner)
async def receiver_remove_cmd(ctx: commands.Context, key: str):
    logger.info(f"Receiver remove command from {ctx.author}: key={key}")
    ok, msg = await delete_receiver(key)
    await ctx.reply(msg)


@receiver_group.command(name="activate", description="Activate a receiver")
@commands.check(is_bot_owner)
async def receiver_activate_cmd(ctx: commands.Context, key: str):
    logger.info(f"Receiver activate command from {ctx.author}: key={key}")
    ok, msg = await set_receiver_active(key, True)
    await ctx.reply(msg)


@receiver_group.command(name="deactivate", description="Deactivate a receiver")
@commands.check(is_bot_owner)
async def receiver_deactivate_cmd(ctx: commands.Context, key: str):
    logger.info(f"Receiver deactivate command from {ctx.author}: key={key}")
    ok, msg = await set_receiver_active(key, False)
    await ctx.reply(msg)


@receiver_group.command(name="config", description="Show receiver configuration")
@commands.check(is_bot_owner)
async def receiver_config_cmd(ctx: commands.Context, key: str):
    logger.info(f"Receiver config command from {ctx.author}: key={key}")
    entry = await get_receiver_entry(key)
    if not entry:
        await ctx.reply(f"Receiver `{key}` not found.")
        return
    pretty = json.dumps(entry, indent=JSON_INDENT)
    await ctx.reply(f"**Receiver `{key}`:**\n```json\n{pretty}\n```")


@receiver_group.command(name="setconfig", description="Set a configuration key for a receiver")
@commands.check(is_bot_owner)
async def receiver_setconfig_cmd(ctx: commands.Context, key: str, config_key: str, *, value: str):
    logger.info(f"Receiver setconfig command from {ctx.author}: {key}.{config_key} = {value}")
    entry = await get_receiver_entry(key)
    if not entry:
        await ctx.reply(f"Receiver `{key}` not found.")
        return
    parsed = _parse_json_arg(value)
    cfg = _deepcopy_cfg(entry["config"])
    cfg[config_key] = parsed
    ok = await update_receiver_config(key, cfg)
    if ok:
        await ctx.reply(f"Set `{config_key}` for receiver `{key}`: `{parsed}`")
    else:
        await ctx.reply("Failed to set configuration.")


@receiver_group.command(name="track", description="Add to receiver include filters")
@commands.check(is_bot_owner)
@discord.app_commands.describe(key="Receiver key", target="What to track (page, user, summary)", value="Pattern or username")
async def receiver_track_cmd(ctx: commands.Context, key: str, target: FilterType, *, value: str):
    logger.info(f"Receiver track command from {ctx.author}: {key} {target.value} += {value}")
    await mutate_filter(ctx, "receivers", key, target, value, action="add_include", entity_label=f"Receiver `{key}`")


@receiver_group.command(name="ignore", description="Add to receiver exclude filters")
@commands.check(is_bot_owner)
@discord.app_commands.describe(key="Receiver key", target="What to ignore (page, user, summary)", value="Pattern or username")
async def receiver_ignore_cmd(ctx: commands.Context, key: str, target: FilterType, *, value: str):
    logger.info(f"Receiver ignore command from {ctx.author}: {key} {target.value} += {value}")
    await mutate_filter(ctx, "receivers", key, target, value, action="add_exclude", entity_label=f"Receiver `{key}`")


@receiver_group.command(name="untrack", description="Remove from receiver include filters")
@commands.check(is_bot_owner)
@discord.app_commands.describe(key="Receiver key", target="What to remove (page, user, summary)", value="Pattern or username")
async def receiver_untrack_cmd(ctx: commands.Context, key: str, target: FilterType, *, value: str):
    logger.info(f"Receiver untrack command from {ctx.author}: {key} {target.value} -= {value}")
    await mutate_filter(ctx, "receivers", key, target, value, action="remove_include", entity_label=f"Receiver `{key}`")


@receiver_group.command(name="unignore", description="Remove from receiver exclude filters")
@commands.check(is_bot_owner)
@discord.app_commands.describe(key="Receiver key", target="What to remove (page, user, summary)", value="Pattern or username")
async def receiver_unignore_cmd(ctx: commands.Context, key: str, target: FilterType, *, value: str):
    logger.info(f"Receiver unignore command from {ctx.author}: {key} {target.value} -= {value}")
    await mutate_filter(ctx, "receivers", key, target, value, action="remove_exclude", entity_label=f"Receiver `{key}`")
