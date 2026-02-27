import discord
from discord.ext import commands

from src.logging_setup import logger
from src.bot_instance import bot
from src.config import (
    ensure_custom_thread_entry,
    mutate_custom_thread_config_list,
    set_custom_thread_active,
)
from src.discord_utils import discord_api_call_with_backoff
from src.commands.helpers import _in_parent_channel, _require_custom_thread


@bot.hybrid_group(name="new",
                  invoke_without_command=True,
                  description="Create new threads",
                  with_app_command=True)
async def new_group(ctx: commands.Context):
    """`/new` (no subcommand) -> show usage."""
    await ctx.reply(
        "**New Thread Commands:**\n"
        "* `/new thread <name>` - Create a custom filter thread\n"
        "* `/new userthread <Username>` - Create a User:<Username> thread and auto-add to userIncludeList"
    )


@new_group.command(name="userthread",
                   description="Create a per-user custom thread and include them")
async def new_userthread_cmd(ctx: commands.Context, *, user: str):
    logger.info(f"Userthread command from {ctx.author} for user '{user}' in {ctx.channel}")
    if not _in_parent_channel(ctx):
        await ctx.reply("Run `/new userthread` in the parent channel.")
        return
    try:
        thread = await discord_api_call_with_backoff(
            ctx.channel.create_thread,
            name=f"User:{user}",
            type=discord.ChannelType.public_thread
        )
        logger.info(f"Created thread {thread.name} ({thread.id}) for user {user}")
    except discord.HTTPException as e:
        logger.error(f"Failed to create thread for user {user}: {e}")
        await ctx.reply(f"Failed to create thread: {e}")
        return
    except Exception as e:
        logger.error(f"Unexpected error creating thread for user {user}: {e}")
        await ctx.reply(f"Failed to create thread: {e}")
        return
    await ensure_custom_thread_entry(str(thread.id), thread.name, create_if_missing=True)
    _, new_list = await mutate_custom_thread_config_list(
        str(thread.id), "userIncludeList", add=[user]
    )
    await ctx.reply(f"Tracking **{user}** in <#{thread.id}>.\n`userIncludeList`: `{new_list}`")


@bot.command(name="add", hidden=True)
async def add_legacy_cmd(ctx: commands.Context, *, user: str):
    """Legacy alias for /new userthread (text command only)."""
    await ctx.invoke(new_userthread_cmd, user=user)


@new_group.command(name="thread",
                   description="Create a custom-filter thread (parent channel only)")
async def new_thread_cmd(ctx: commands.Context, *, threadname: str = ""):
    logger.info(f"Thread command from {ctx.author} for thread '{threadname}' in {ctx.channel}")
    if not _in_parent_channel(ctx):
        await ctx.reply("Use `/new thread` in the parent channel.")
        return
    if not threadname.strip():
        await ctx.reply("Please provide a thread name: `/new thread <name>`.")
        return
    try:
        thread = await discord_api_call_with_backoff(
            ctx.channel.create_thread,
            name=threadname,
            type=discord.ChannelType.public_thread
        )
        logger.info(f"Created custom thread {thread.name} ({thread.id})")
    except discord.HTTPException as e:
        logger.error(f"Failed to create custom thread '{threadname}': {e}")
        await ctx.reply(f"Failed to create thread: {e}")
        return
    except Exception as e:
        logger.error(f"Unexpected error creating custom thread '{threadname}': {e}")
        await ctx.reply(f"Failed to create thread: {e}")
        return
    await ensure_custom_thread_entry(str(thread.id), thread.name, create_if_missing=True)
    await ctx.reply(f"Thread created: <#{thread.id}> (active). Configure with `/config` inside the thread.")


@bot.command(name="addcustom", hidden=True)
async def addcustom_legacy_cmd(ctx: commands.Context, *, threadname: str = ""):
    """Legacy alias for /new thread (text command only)."""
    await ctx.invoke(new_thread_cmd, threadname=threadname)


@bot.hybrid_command(name="activate",
                    description="Activate current custom thread",
                    with_app_command=True)
async def activate_custom_thread_cmd(ctx: commands.Context):
    logger.info(f"Activate command from {ctx.author} in thread {ctx.channel.id}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    ok = await set_custom_thread_active(str(ctx.channel.id), True)
    if ok:
        await ctx.reply("Activated.")
    else:
        await ctx.reply("Failed to activate (missing config?).")


@bot.hybrid_command(name="deactivate",
                    description="Deactivate current custom thread",
                    with_app_command=True)
async def deactivate_custom_thread_cmd(ctx: commands.Context):
    logger.info(f"Deactivate command from {ctx.author} in thread {ctx.channel.id}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    ok = await set_custom_thread_active(str(ctx.channel.id), False)
    if ok:
        await ctx.reply("Deactivated.")
    else:
        await ctx.reply("Failed to deactivate (missing config?).")
