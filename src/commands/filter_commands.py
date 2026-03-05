import discord
from discord.ext import commands

from src.logging_setup import logger
from src.bot_instance import bot
from src.commands.helpers import (
    FilterType,
    _require_custom_thread,
    mutate_filter,
)


@bot.hybrid_command(name="track",
                    description="Add to include filters (shortcut for /config add)",
                    with_app_command=True)
@discord.app_commands.describe(target="What to track (page, user, summary)", value="Pattern or username")
async def track_cmd(ctx: commands.Context, target: FilterType, *, value: str):
    logger.info(f"Track command from {ctx.author}: {target.value} {value}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    await mutate_filter(ctx, "threads", str(ctx.channel.id), target, value, action="add_include")


@bot.hybrid_command(name="ignore",
                    description="Add to exclude filters (shortcut for /config add)",
                    with_app_command=True)
@discord.app_commands.describe(target="What to ignore (page, user, summary)", value="Pattern or username")
async def ignore_cmd(ctx: commands.Context, target: FilterType, *, value: str):
    logger.info(f"Ignore command from {ctx.author}: {target.value} {value}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    await mutate_filter(ctx, "threads", str(ctx.channel.id), target, value, action="add_exclude")


@bot.hybrid_command(name="untrack",
                    description="Remove from include filters (shortcut for /config remove)",
                    with_app_command=True)
@discord.app_commands.describe(target="What to remove (page, user, summary)", value="Pattern or username")
async def untrack_cmd(ctx: commands.Context, target: FilterType, *, value: str):
    logger.info(f"Untrack command from {ctx.author}: {target.value} {value}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    await mutate_filter(ctx, "threads", str(ctx.channel.id), target, value, action="remove_include")


@bot.hybrid_command(name="unignore",
                    description="Remove from exclude filters (shortcut for /config remove)",
                    with_app_command=True)
@discord.app_commands.describe(target="What to remove (page, user, summary)", value="Pattern or username")
async def unignore_cmd(ctx: commands.Context, target: FilterType, *, value: str):
    logger.info(f"Unignore command from {ctx.author}: {target.value} {value}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    await mutate_filter(ctx, "threads", str(ctx.channel.id), target, value, action="remove_exclude")
