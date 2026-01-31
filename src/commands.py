import discord
from discord.ext import commands
import json
import io
from enum import Enum
from typing import Any, Optional, List

from src.logging_setup import logger
from src.bot_instance import bot
from src.constants import JSON_INDENT, UTF8_ENCODING
from src.config import (
    CONFIG,
    CONFIG_LOCK,
    DISCORD_CHANNEL_ID,
    OWNER_ID,
    save_config,
    ensure_custom_thread_entry,
    mutate_custom_thread_config_list,
    set_custom_thread_active,
    update_custom_thread_config,
    create_receiver,
    delete_receiver,
    set_receiver_active,
    get_receiver_entry,
    update_receiver_config,
    mutate_receiver_config_list,
    _normalise_list_val,
    WEBHOOKS,
)
from src.discord_utils import discord_api_call_with_backoff

# --------------------------------------------------------------------------- #
# ── Helper functions                                                         #
# --------------------------------------------------------------------------- #

def authorised(ctx) -> bool:
    """
    Gatekeeper: only allow the bot owner.
    """
    return ctx.author.id == OWNER_ID

def is_bot_owner(ctx) -> bool:
    """Check if user is the bot owner."""
    return ctx.author.id == OWNER_ID

def _in_parent_channel(ctx: commands.Context) -> bool:
    return ctx.channel.id == DISCORD_CHANNEL_ID

def _in_custom_thread(ctx: commands.Context) -> bool:
    """True when inside *any* thread spawned from the parent channel."""
    return (
        isinstance(ctx.channel, discord.Thread)
        and ctx.channel.parent_id == DISCORD_CHANNEL_ID
    )

def _parse_json_arg(s: str) -> Any:
    try:
        return json.loads(s)
    except Exception:
        return s  # fall back to raw string

def _deepcopy_cfg(obj: Any) -> Any:
    """Cheap JSON round-trip copy."""
    try:
        return json.loads(json.dumps(obj))
    except Exception:
        # worst case, return original; caller should treat as immutable
        return obj

async def _require_custom_thread(ctx: commands.Context) -> Optional[dict]:
    if not _in_custom_thread(ctx):
        await ctx.reply("This command must be used inside a thread.")
        return None
    entry = await ensure_custom_thread_entry(str(ctx.channel.id), ctx.channel.name, create_if_missing=False)
    if entry is None:
        # thread exists but no entry (perhaps race); create one
        entry = await ensure_custom_thread_entry(str(ctx.channel.id), ctx.channel.name, create_if_missing=True)
    return entry

class FilterType(Enum):
    PAGE = "page"
    USER = "user"
    SUMMARY = "summary"

# Mapping from shortcut target to config key
_INCLUDE_KEY_MAP = {
    FilterType.PAGE: "pageIncludePatterns",
    FilterType.USER: "userIncludeList",
    FilterType.SUMMARY: "summaryIncludePatterns",
}

_EXCLUDE_KEY_MAP = {
    FilterType.PAGE: "pageExcludePatterns",
    FilterType.USER: "userExcludeList",
    FilterType.SUMMARY: "summaryExcludePatterns",
}

# --------------------------------------------------------------------------- #
# ── Commands                                                                 #
# --------------------------------------------------------------------------- #

@bot.hybrid_command(name="fezhelp",
                    description="Show available commands",
                    with_app_command=True)
async def fezhelp_cmd(ctx: commands.Context):
    """Display available commands and their descriptions."""
    logger.info(f"Help command from {ctx.author} in {ctx.channel}")
    
    chunks = [
        """**Available Commands:**

**Basic Commands:**
* `/fezhelp` - Show this help message
* `/status` - Show current thread configuration

**Thread Management (run in parent channel):**
* `/new thread <name>` - Create a new tracking thread
* `/new userthread <Username>` - Create a "User:Username" thread and auto-add to userIncludeList
* `/activate` - Activate current thread
* `/deactivate` - Deactivate current thread""",

        """**Tracking Shortcuts (run inside a thread):**
* `/track page|user|summary <value>` - Add to include filters
* `/ignore page|user|summary <value>` - Add to exclude filters
* `/untrack page|user|summary <value>` - Remove from include filters
* `/unignore page|user|summary <value>` - Remove from exclude filters

**Global Configuration:**
* `/globalconfig getraw` - Download full configuration as JSON
* `/globalconfig setraw [attachment]` - Replace configuration from attached JSON file (DANGEROUS)""",

        """**Advanced Thread Configuration:**
* `/config` - Show current thread configuration (same as `/status`)
* `/config set <key> <json>` - Set configuration value
* `/config add <key> <value>` - Add to list configuration
* `/config remove <key> <value>` - Remove from list configuration
* `/config clear <key>` - Clear list configuration
* `/config getraw` - Download raw JSON for current thread
* `/config setraw [attachment]` - Replace current-thread configuration from attached JSON (DANGEROUS)""",

        """**Receiver Commands (owner only - for webhook feeds):**
* `/receiver list` - List all receivers
* `/receiver add <key> <name>` - Create a new receiver
* `/receiver remove <key>` - Delete a receiver
* `/receiver activate <key>` / `/receiver deactivate <key>` - Toggle receiver
* `/receiver config <key>` - Show receiver configuration
* `/receiver track|ignore|untrack|unignore <key> page|user|summary <value>` - Manage filters

**Configuration Keys:**
* `siteName` - Filter by site (e.g., "en.wikipedia.org")
* `pageIncludePatterns` / `pageExcludePatterns` - Page regex patterns
* `userIncludeList` / `userExcludeList` - User lists
* `summaryIncludePatterns` / `summaryExcludePatterns` - Summary regex patterns

**Legacy Commands (still work):**
* `!add` (→ `/new userthread`), `!addcustom` (→ `/new thread`), `!activate`, `!deactivate`, `!config`"""
    ]

    for i, chunk in enumerate(chunks):
        if i == 0:
            await ctx.reply(chunk)
        else:
            await ctx.send(chunk)


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
    """
    Convenience wrapper that creates a User:<username> thread,
    then appends the supplied username to `userIncludeList`.
    """
    logger.info(f"Userthread command from {ctx.author} for user '{user}' in {ctx.channel}")

    if ctx.channel.id != DISCORD_CHANNEL_ID:
        logger.warning(f"Userthread command attempted in wrong channel: {ctx.channel.id} (expected {DISCORD_CHANNEL_ID})")
        await ctx.reply("Run `/new userthread` in the parent channel.")
        return

    # create "User:<Username>" thread
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


# Legacy text command for backward compatibility with !add
@bot.command(name="add", hidden=True)
async def add_legacy_cmd(ctx: commands.Context, *, user: str):
    """Legacy alias for /new userthread (text command only)."""
    await ctx.invoke(new_userthread_cmd, user=user)


# --------------------------------------------------------------------------- #
# ── Global configuration ("/globalconfig ...")                                 #
# --------------------------------------------------------------------------- #

@bot.hybrid_group(name="globalconfig",
                  invoke_without_command=True,
                  description="Global configuration operations",
                  with_app_command=True)
@commands.check(authorised)
async def globalconfig_group(ctx: commands.Context):
    """`/globalconfig` (no subcommand) -> `/globalconfig getraw`."""
    await ctx.invoke(globalconfig_getraw_cmd)


@globalconfig_group.command(name="getraw",
                            description="Download full configuration as JSON")
@commands.check(authorised)
async def globalconfig_getraw_cmd(ctx: commands.Context):  # noqa: N802
    logger.info(f"Global config getraw command from {ctx.author}")
    async with CONFIG_LOCK:
        data = json.dumps(CONFIG, indent=JSON_INDENT).encode(UTF8_ENCODING)
    file = discord.File(io.BytesIO(data), filename="global_config.json")
    await ctx.reply(file=file, mention_author=False)


@globalconfig_group.command(name="setraw",
                            description="Replace configuration from attached JSON (DANGEROUS)")
@commands.check(authorised)
async def globalconfig_setraw_cmd(ctx: commands.Context, attachment: discord.Attachment = None):  # noqa: N802
    logger.warning(f"Global config setraw command from {ctx.author} - DANGEROUS OPERATION")
    
    # Handle both slash command (attachment parameter) and message command (ctx.message.attachments)
    if attachment is None:
        # Legacy message command path - check if ctx.message exists and has attachments
        if ctx.message is None or not ctx.message.attachments:
            logger.warning("Global config setraw attempted without attachment")
            await ctx.reply("Attach a **JSON** file to `/globalconfig setraw`.", mention_author=False)
            return
        attachment = ctx.message.attachments[0]
    
    try:
        raw = await attachment.read()
        new_cfg = json.loads(raw)
        logger.info(f"Successfully parsed global config from attachment: {len(new_cfg.get('threads', {}))} threads")
    except Exception as e:
        logger.error(f"Failed to parse global config attachment: {e}")
        await ctx.reply(f"Could not parse attachment as JSON: {e}", mention_author=False)
        return
    async with CONFIG_LOCK:
        CONFIG.clear()
        CONFIG.update(new_cfg)
        save_config(CONFIG)
    logger.info("Global configuration replaced successfully")
    await ctx.reply("Global configuration **replaced**.", mention_author=False)


# --------------------------------------------------------------------------- #
# ── Custom thread commands                                                   #
# --------------------------------------------------------------------------- #

@new_group.command(name="thread",
                   description="Create a custom-filter thread (parent channel only)")
async def new_thread_cmd(ctx: commands.Context, *, threadname: str = ""):
    """/new thread <threadname> → create a custom filter thread (in parent channel only)."""
    logger.info(f"Thread command from {ctx.author} for thread '{threadname}' in {ctx.channel}")

    if not _in_parent_channel(ctx):
        logger.warning(f"Thread command attempted in wrong channel: {ctx.channel.id}")
        await ctx.reply("Use `/new thread` in the parent channel.")
        return
    if not threadname.strip():
        logger.warning("Thread command attempted without thread name")
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


# Legacy text command for backward compatibility with !addcustom
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
        logger.error(f"Failed to activate thread {ctx.channel.id}")
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
        logger.error(f"Failed to deactivate thread {ctx.channel.id}")
        await ctx.reply("Failed to deactivate (missing config?).")


@bot.hybrid_group(name="config",
                  invoke_without_command=True,
                  description="Thread configuration operations",
                  with_app_command=True)
async def config_group(ctx: commands.Context):
    """`/config` (no subcommand) -> `/config get`."""
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
    parsed = _parse_json_arg(value)
    # If key unknown, set anyway
    cfg = _deepcopy_cfg(entry["config"])
    cfg[key] = parsed
    ok = await update_custom_thread_config(str(ctx.channel.id), cfg)
    if ok:
        await ctx.reply(f"Set `{key}`: `{parsed}`")
    else:
        logger.error(f"Failed to set config {key} for thread {ctx.channel.id}")
        await ctx.reply("Failed to set.")


@config_group.command(name="add",
                      description="Append value(s) to a list key")
async def config_add_cmd(ctx: commands.Context, key: str, *, value: str):
    logger.info(f"Config add command from {ctx.author} in thread {ctx.channel.id}: {key} += {value}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    vals = _normalise_list_val(_parse_json_arg(value))
    ok, new_list = await mutate_custom_thread_config_list(str(ctx.channel.id), key, add=vals)
    if ok:
        await ctx.reply(f"Added to `{key}`: `{vals}`\nNow: `{new_list}`")
    else:
        logger.error(f"Failed to add to config {key} for thread {ctx.channel.id}")
        await ctx.reply("Failed to add.")


@config_group.command(name="remove",
                      description="Remove value(s) from a list key")
async def config_remove_cmd(ctx: commands.Context, key: str, *, value: str):
    logger.info(f"Config remove command from {ctx.author} in thread {ctx.channel.id}: {key} -= {value}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    vals = _normalise_list_val(_parse_json_arg(value))
    ok, new_list = await mutate_custom_thread_config_list(str(ctx.channel.id), key, remove=vals)
    if ok:
        await ctx.reply(f"Removed from `{key}`: `{vals}`\nNow: `{new_list}`")
    else:
        logger.error(f"Failed to remove from config {key} for thread {ctx.channel.id}")
        await ctx.reply("Failed to remove.")


@config_group.command(name="clear",
                      description="Clear a list key")
async def config_clear_cmd(ctx: commands.Context, key: str):
    logger.info(f"Config clear command from {ctx.author} in thread {ctx.channel.id}: clear {key}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    ok, _ = await mutate_custom_thread_config_list(str(ctx.channel.id), key, clear=True)
    if ok:
        await ctx.reply(f"Cleared `{key}`.")
    else:
        logger.error(f"Failed to clear config {key} for thread {ctx.channel.id}")
        await ctx.reply("Failed to clear.")


# --------------------------------------------------------------------------- #
# ── /config … raw variants (per‑thread)                                     #
# --------------------------------------------------------------------------- #

@config_group.command(name="getraw",
                      description="Download raw JSON configuration for this thread")
@commands.check(authorised)
async def config_getraw_cmd(ctx: commands.Context):
    logger.info(f"Config getraw command from {ctx.author} in thread {ctx.channel.id}")
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
    logger.warning(f"Config setraw command from {ctx.author} in thread {ctx.channel.id}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return

    # Handle both slash command (attachment parameter) and message command (ctx.message.attachments)
    if attachment is None:
        # Legacy message command path - check if ctx.message exists and has attachments
        if ctx.message is None or not ctx.message.attachments:
            await ctx.reply("Attach a **JSON** file to `/config setraw`.",
                            mention_author=False)
            return
        attachment = ctx.message.attachments[0]

    try:
        raw = await attachment.read()
        new_cfg = json.loads(raw)
        logger.info(f"Parsed thread config for {ctx.channel.id} successfully")
    except Exception as e:
        logger.error(f"Failed to parse thread config: {e}")
        await ctx.reply(f"Could not parse attachment as JSON: {e}",
                        mention_author=False)
        return

    ok = await update_custom_thread_config(str(ctx.channel.id), new_cfg)
    if ok:
        await ctx.reply("Thread configuration **replaced**.",
                        mention_author=False)
    else:
        await ctx.reply("Failed to replace configuration.",
                        mention_author=False)

# --------------------------------------------------------------------------- #
# ── Tracking shortcut commands                                               #
# --------------------------------------------------------------------------- #

@bot.hybrid_command(name="track",
                    description="Add to include filters (shortcut for /config add)",
                    with_app_command=True)
@discord.app_commands.describe(target="What to track (page, user, summary)", value="Pattern or username")
async def track_cmd(ctx: commands.Context, target: FilterType, *, value: str):
    logger.info(f"Track command from {ctx.author}: {target.value} {value}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return

    key = _INCLUDE_KEY_MAP[target]
    vals = _normalise_list_val(value)
    ok, new_list = await mutate_custom_thread_config_list(
        str(ctx.channel.id), key, add=vals
    )
    if ok:
        await ctx.reply(f"Now tracking {target.value}: `{vals}`\n`{key}`: `{new_list}`")
    else:
        await ctx.reply(f"Failed to add {target.value}.")


@bot.hybrid_command(name="ignore",
                    description="Add to exclude filters (shortcut for /config add)",
                    with_app_command=True)
@discord.app_commands.describe(target="What to ignore (page, user, summary)", value="Pattern or username")
async def ignore_cmd(ctx: commands.Context, target: FilterType, *, value: str):
    logger.info(f"Ignore command from {ctx.author}: {target.value} {value}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return

    key = _EXCLUDE_KEY_MAP[target]
    vals = _normalise_list_val(value)
    ok, new_list = await mutate_custom_thread_config_list(
        str(ctx.channel.id), key, add=vals
    )
    if ok:
        await ctx.reply(f"Now ignoring {target.value}: `{vals}`\n`{key}`: `{new_list}`")
    else:
        await ctx.reply(f"Failed to ignore {target.value}.")


@bot.hybrid_command(name="untrack",
                    description="Remove from include filters (shortcut for /config remove)",
                    with_app_command=True)
@discord.app_commands.describe(target="What to remove (page, user, summary)", value="Pattern or username")
async def untrack_cmd(ctx: commands.Context, target: FilterType, *, value: str):
    logger.info(f"Untrack command from {ctx.author}: {target.value} {value}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return

    key = _INCLUDE_KEY_MAP[target]
    vals = _normalise_list_val(value)
    ok, new_list = await mutate_custom_thread_config_list(
        str(ctx.channel.id), key, remove=vals
    )
    if ok:
        await ctx.reply(f"Removed {target.value}: `{vals}`\n`{key}`: `{new_list}`")
    else:
        await ctx.reply(f"Failed to remove {target.value}.")


@bot.hybrid_command(name="unignore",
                    description="Remove from exclude filters (shortcut for /config remove)",
                    with_app_command=True)
@discord.app_commands.describe(target="What to remove (page, user, summary)", value="Pattern or username")
async def unignore_cmd(ctx: commands.Context, target: FilterType, *, value: str):
    logger.info(f"Unignore command from {ctx.author}: {target.value} {value}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return

    key = _EXCLUDE_KEY_MAP[target]
    vals = _normalise_list_val(value)
    ok, new_list = await mutate_custom_thread_config_list(
        str(ctx.channel.id), key, remove=vals
    )
    if ok:
        await ctx.reply(f"Removed {target.value} from exclude list: `{vals}`\n`{key}`: `{new_list}`")
    else:
        await ctx.reply(f"Failed to remove {target.value}.")


# --------------------------------------------------------------------------- #
# ── Status command (alias for /config get)                                   #
# --------------------------------------------------------------------------- #

@bot.hybrid_command(name="status",
                    description="Show current thread configuration",
                    with_app_command=True)
async def status_cmd(ctx: commands.Context):
    """Alias for `/config get` - shows current thread configuration."""
    logger.info(f"Status command from {ctx.author} in thread {ctx.channel.id}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    pretty = json.dumps(entry["config"], indent=JSON_INDENT)
    await ctx.reply(f"```json\n{pretty}\n```")


# --------------------------------------------------------------------------- #
# ── Receiver (webhook) commands                                              #
# --------------------------------------------------------------------------- #

@bot.hybrid_group(name="receiver",
                  invoke_without_command=True,
                  description="Manage webhook receivers (owner only)",
                  with_app_command=True)
@commands.check(is_bot_owner)
async def receiver_group(ctx: commands.Context):
    """`/receiver` (no subcommand) -> show usage."""
    await ctx.reply(
        "**Receiver Commands (owner only):**\n"
        "* `/receiver list` - List all receivers\n"
        "* `/receiver add <key> <name>` - Create a new receiver\n"
        "* `/receiver remove <key>` - Delete a receiver\n"
        "* `/receiver activate <key>` - Activate a receiver\n"
        "* `/receiver deactivate <key>` - Deactivate a receiver\n"
        "* `/receiver config <key>` - Show receiver config\n"
        "* `/receiver track <key> page|user|summary <value>` - Add to include filters\n"
        "* `/receiver ignore <key> page|user|summary <value>` - Add to exclude filters\n"
        "* `/receiver untrack <key> page|user|summary <value>` - Remove from include filters\n"
        "* `/receiver unignore <key> page|user|summary <value>` - Remove from exclude filters"
    )


@receiver_group.command(name="list",
                        description="List all receivers")
@commands.check(is_bot_owner)
async def receiver_list_cmd(ctx: commands.Context):
    """List all configured receivers."""
    logger.info(f"Receiver list command from {ctx.author}")
    async with CONFIG_LOCK:
        receivers = CONFIG.get("receivers", {})

    if not receivers:
        await ctx.reply("No receivers configured.")
        return

    lines = ["**Configured Receivers:**"]
    for key, entry in receivers.items():
        status_parts = []
        if entry.get("active"):
            status_parts.append("active")
        else:
            status_parts.append("inactive")
        if entry.get("errored"):
            status_parts.append("ERRORED")
        has_webhook = "webhook" if key in WEBHOOKS else "NO WEBHOOK"
        status = ", ".join(status_parts)
        lines.append(f"* `{key}`: {entry.get('name', '(unnamed)')} [{status}, {has_webhook}]")

    await ctx.reply("\n".join(lines))


@receiver_group.command(name="add",
                        description="Create a new receiver")
@commands.check(is_bot_owner)
async def receiver_add_cmd(ctx: commands.Context, key: str, *, name: str):
    """Create a new receiver with the given key and name."""
    logger.info(f"Receiver add command from {ctx.author}: key={key}, name={name}")
    ok, msg = await create_receiver(key, name)
    await ctx.reply(msg)


@receiver_group.command(name="remove",
                        description="Delete a receiver")
@commands.check(is_bot_owner)
async def receiver_remove_cmd(ctx: commands.Context, key: str):
    """Delete a receiver by key."""
    logger.info(f"Receiver remove command from {ctx.author}: key={key}")
    ok, msg = await delete_receiver(key)
    await ctx.reply(msg)


@receiver_group.command(name="activate",
                        description="Activate a receiver")
@commands.check(is_bot_owner)
async def receiver_activate_cmd(ctx: commands.Context, key: str):
    """Activate a receiver by key."""
    logger.info(f"Receiver activate command from {ctx.author}: key={key}")
    ok, msg = await set_receiver_active(key, True)
    await ctx.reply(msg)


@receiver_group.command(name="deactivate",
                        description="Deactivate a receiver")
@commands.check(is_bot_owner)
async def receiver_deactivate_cmd(ctx: commands.Context, key: str):
    """Deactivate a receiver by key."""
    logger.info(f"Receiver deactivate command from {ctx.author}: key={key}")
    ok, msg = await set_receiver_active(key, False)
    await ctx.reply(msg)


@receiver_group.command(name="config",
                        description="Show receiver configuration")
@commands.check(is_bot_owner)
async def receiver_config_cmd(ctx: commands.Context, key: str):
    """Show configuration for a receiver."""
    logger.info(f"Receiver config command from {ctx.author}: key={key}")
    entry = await get_receiver_entry(key)
    if not entry:
        await ctx.reply(f"Receiver `{key}` not found.")
        return
    pretty = json.dumps(entry, indent=JSON_INDENT)
    await ctx.reply(f"**Receiver `{key}`:**\n```json\n{pretty}\n```")


@receiver_group.command(name="setconfig",
                        description="Set a configuration key for a receiver")
@commands.check(is_bot_owner)
async def receiver_setconfig_cmd(ctx: commands.Context, key: str, config_key: str, *, value: str):
    """Set a configuration value for a receiver."""
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


# --------------------------------------------------------------------------- #
# ── Receiver track/ignore commands                                            #
# --------------------------------------------------------------------------- #

@receiver_group.command(name="track",
                        description="Add to receiver include filters")
@commands.check(is_bot_owner)
@discord.app_commands.describe(key="Receiver key", target="What to track (page, user, summary)", value="Pattern or username")
async def receiver_track_cmd(ctx: commands.Context, key: str, target: FilterType, *, value: str):
    """Add to receiver's include list."""
    logger.info(f"Receiver track command from {ctx.author}: {key} {target.value} += {value}")

    config_key = _INCLUDE_KEY_MAP[target]
    vals = _normalise_list_val(value)
    ok, new_list = await mutate_receiver_config_list(key, config_key, add=vals)
    if ok:
        await ctx.reply(f"Receiver `{key}` now tracking {target.value}: `{vals}`\n`{config_key}`: `{new_list}`")
    else:
        await ctx.reply(f"Receiver `{key}` not found.")


@receiver_group.command(name="ignore",
                        description="Add to receiver exclude filters")
@commands.check(is_bot_owner)
@discord.app_commands.describe(key="Receiver key", target="What to ignore (page, user, summary)", value="Pattern or username")
async def receiver_ignore_cmd(ctx: commands.Context, key: str, target: FilterType, *, value: str):
    """Add to receiver's exclude list."""
    logger.info(f"Receiver ignore command from {ctx.author}: {key} {target.value} += {value}")

    config_key = _EXCLUDE_KEY_MAP[target]
    vals = _normalise_list_val(value)
    ok, new_list = await mutate_receiver_config_list(key, config_key, add=vals)
    if ok:
        await ctx.reply(f"Receiver `{key}` now ignoring {target.value}: `{vals}`\n`{config_key}`: `{new_list}`")
    else:
        await ctx.reply(f"Receiver `{key}` not found.")


@receiver_group.command(name="untrack",
                        description="Remove from receiver include filters")
@commands.check(is_bot_owner)
@discord.app_commands.describe(key="Receiver key", target="What to remove (page, user, summary)", value="Pattern or username")
async def receiver_untrack_cmd(ctx: commands.Context, key: str, target: FilterType, *, value: str):
    """Remove from receiver's include list."""
    logger.info(f"Receiver untrack command from {ctx.author}: {key} {target.value} -= {value}")

    config_key = _INCLUDE_KEY_MAP[target]
    vals = _normalise_list_val(value)
    ok, new_list = await mutate_receiver_config_list(key, config_key, remove=vals)
    if ok:
        await ctx.reply(f"Removed {target.value} from receiver `{key}`: `{vals}`\n`{config_key}`: `{new_list}`")
    else:
        await ctx.reply(f"Receiver `{key}` not found.")


@receiver_group.command(name="unignore",
                        description="Remove from receiver exclude filters")
@commands.check(is_bot_owner)
@discord.app_commands.describe(key="Receiver key", target="What to remove (page, user, summary)", value="Pattern or username")
async def receiver_unignore_cmd(ctx: commands.Context, key: str, target: FilterType, *, value: str):
    """Remove from receiver's exclude list."""
    logger.info(f"Receiver unignore command from {ctx.author}: {key} {target.value} -= {value}")

    config_key = _EXCLUDE_KEY_MAP[target]
    vals = _normalise_list_val(value)
    ok, new_list = await mutate_receiver_config_list(key, config_key, remove=vals)
    if ok:
        await ctx.reply(f"Removed {target.value} from receiver `{key}` exclude list: `{vals}`\n`{config_key}`: `{new_list}`")
    else:
        await ctx.reply(f"Receiver `{key}` not found.")
