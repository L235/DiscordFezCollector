#!/usr/bin/env python3
"""
fez_collector — Discord edition
--------------------------------
* Monitors MediaWiki EventStreams and posts changes to Discord.
* Reads **and** persists state (config) to a JSON file whose location is
  supplied in the `FEZ_COLLECTOR_STATE` environment variable.
* Lets authorised users update the config live from Discord commands.
* **Config schema (per‑user threads):**

      {
          "users": {
              "Username": {
                  "included": true,
                  "thread_id": 123456789012345678
              }
          }
      }

  When a user is *included*, a dedicated thread is (lazily) created in the
  configured parent channel and its ID recorded. Unincluding a user preserves
  the thread mapping for later reuse.
"""
VERSION = "0.5-discord"
print(f"fez_collector {VERSION} initialising…")

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands
from pywikibot import Site
from pywikibot.comms.eventstreams import EventStreams

# --------------------------------------------------------------------------- #
# ── Environment / runtime configuration                                      #
# --------------------------------------------------------------------------- #

DISCORD_TOKEN      = os.getenv("FEZ_COLLECTOR_DISCORD_TOKEN")
DISCORD_CHANNEL_ID = int(os.getenv("FEZ_COLLECTOR_CHANNEL_ID", "0"))  # numeric
STATE_FILE         = Path(os.getenv("FEZ_COLLECTOR_STATE", "./state/config.json"))
STALENESS_SECS     = 2 * 60 * 60  # two hours

if not DISCORD_TOKEN or not DISCORD_CHANNEL_ID:
    sys.exit("❌  FEZ_COLLECTOR_DISCORD_TOKEN or FEZ_COLLECTOR_CHANNEL_ID missing")

# --------------------------------------------------------------------------- #
# ── Config management                                                        #
# --------------------------------------------------------------------------- #

# Config schema: {"users": {"Username": {"included": bool, "thread_id": int|None}}}
DEFAULT_CONFIG = {"users": {}}





def load_config() -> dict:
    if not STATE_FILE.exists():
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    with STATE_FILE.open(encoding="utf-8") as fp:
        raw = json.load(fp)
    return raw


def save_config(cfg: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(cfg, fp, indent=2, sort_keys=True)
    tmp.replace(STATE_FILE)


CONFIG = load_config()
CONFIG_LOCK = asyncio.Lock()  # to prevent simultaneous writes

# --------------------------------------------------------------------------- #
# ── MediaWiki EventStreams setup                                             #
# --------------------------------------------------------------------------- #

site = Site("en", "wikipedia")
stream = EventStreams(
    streams=["recentchange", "revision-create"],
    since=datetime.utcnow().isoformat()
)
stream.register_filter(server_name="en.wikipedia.org")

# --------------------------------------------------------------------------- #
# ── Discord bot setup                                                        #
# --------------------------------------------------------------------------- #

intents = discord.Intents.default()
intents.message_content = True  # needed for on_message / commands with prefix
bot = commands.Bot(command_prefix="!", intents=intents)

# --------------------------------------------------------------------------- #
# ── Message formatting                                                        #
# --------------------------------------------------------------------------- #

def format_change(change: dict) -> str:
    """Turn one EventStreams record into a concise Discord message."""
    user = change['user']
    title = change["title"]
    comment = change.get("comment", "") or "(no summary)"
    
    if change["type"] == "log":
        link = (
            f"https://{change['server_name']}/w/index.php?title=Special:Log"
            f"&logid={change['log_id']}"
        )
        return f"**{user}** {change['log_action_comment']} \n<{link}>"

    # For regular edits
    page_url = f"https://{change['server_name']}/wiki/{title.replace(' ', '_')}"
    
    # Handle different change types that may or may not have revision info
    if change["type"] == "edit" and "revision" in change:
        diff_url = f"https://{change['server_name']}/w/index.php?diff={change['revision']['new']}"
        return f"**{user}** edited **[{title}](<{page_url}>)** ({comment}) \n<{diff_url}>"
    elif change["type"] == "new":
        return f"**{user}** created **[{title}](<{page_url}>)** ({comment})"
    elif change["type"] == "categorize":
        return f"**{user}** categorized **[{title}](<{page_url}>)** ({comment})"
    else:
        # Fallback for other change types
        return f"**{user}** modified **[{title}](<{page_url}>)** ({comment})"

# --------------------------------------------------------------------------- #
# ── EventStreams processing                                                  #
# --------------------------------------------------------------------------- #

async def stream_worker(channel: discord.TextChannel):
    """Background task: tail MediaWiki EventStreams and route posts to threads."""
    loop = asyncio.get_running_loop()

    async def get_user_thread(user: str) -> discord.abc.Messageable:
        """Get the thread for a user, falling back to parent if thread doesn't exist."""
        thread = await ensure_user_thread(user, parent_channel=channel, create=False)
        # ensure_user_thread() returns None if user not included; parent channel if thread not found
        return thread

    async def should_post(user: str) -> bool:
        async with CONFIG_LOCK:
            entry = CONFIG["users"].get(user)
            return bool(entry and entry.get("included", False))

    for change in stream:
        # Let Discord breathe
        await asyncio.sleep(0)  # cooperative yield

        ts     = float(change["timestamp"])
        current_time = datetime.now(timezone.utc).timestamp()
        stale  = (current_time - ts) > STALENESS_SECS
        user   = change["user"]
        if stale or not await should_post(user):
            continue

        msg = format_change(change)
        if len(msg) > 2000:           # Discord hard limit
            msg = msg[:1990] + "…"
        target = await get_user_thread(user)
        if target is None:
            # user not included, skip (defensive; should not reach)
            continue
        await loop.create_task(target.send(msg))

# --------------------------------------------------------------------------- #
# ── Discord commands                                                         #
# --------------------------------------------------------------------------- #

def authorised(ctx) -> bool:
    """Gatekeeper: only allow guild moderators or the bot owner."""
    return ctx.author.guild_permissions.manage_guild or ctx.author.id == bot.owner_id


@bot.command(name="ping")
async def ping_cmd(ctx: commands.Context):
    await ctx.reply("pong")


@bot.command(name="fezquit")
@commands.check(authorised)
async def quit_cmd(ctx: commands.Context):
    await ctx.message.add_reaction("👋")
    await bot.close()


async def _set_user_included(ctx, user: str, included: bool):
    """
    Mark a user as included/unincluded.
    When including: eagerly create (or reuse) that user's thread immediately,
    storing its ID in config. On remove we retain the thread mapping.
    """
    async with CONFIG_LOCK:
        entry = CONFIG["users"].get(user)
        if entry is None:
            entry = {"included": included, "thread_id": None}
            CONFIG["users"][user] = entry
            action = "created & set"
        else:
            prev = entry.get("included", False)
            if prev == included:
                await ctx.reply(f"`{user}` already {'added' if included else 'removed'}.")
                return
            entry["included"] = included
            action = "updated"
        save_config(CONFIG)

    # Eager thread creation when including
    if included:
        parent_channel = ctx.guild.get_channel(DISCORD_CHANNEL_ID)
        if parent_channel is None:
            parent_channel = await bot.fetch_channel(DISCORD_CHANNEL_ID)
        thread_chan = await ensure_user_thread(user, parent_channel=parent_channel, create=True)
        if thread_chan is None:
            await ctx.reply(f"⚠️  Added `{user}` but user not in config? (race) Saved anyway.")
            return
        if thread_chan == parent_channel:
            await ctx.reply(f"⚠️  Added `{user}`, but could not create/find a thread; will post in parent channel.")
            return
        await ctx.reply(f"`{user}` {action} to added. Thread: <#{thread_chan.id}>.")
        return

    # Removed path
    await ctx.reply(f"`{user}` {action} to removed (thread retained).")


@bot.command(name="add")
@commands.check(authorised)
async def add_cmd(ctx: commands.Context, *, user: str):
    """!add <Username> → start tracking a user (creates/uses dedicated thread)."""
    await _set_user_included(ctx, user, True)


@bot.command(name="remove")
@commands.check(authorised)
async def remove_cmd(ctx: commands.Context, *, user: str):
    """!remove <Username> → stop tracking a user (thread retained)."""
    await _set_user_included(ctx, user, False)


@bot.command(name="showconfig")
@commands.check(authorised)
async def showconfig_cmd(ctx: commands.Context):
    """Dump the current config."""
    async with CONFIG_LOCK:
        pretty = json.dumps(CONFIG, indent=2)
    await ctx.reply(f"```json\n{pretty}\n```")


# --------------------------------------------------------------------------- #
# ── Thread utilities                                                         #
# --------------------------------------------------------------------------- #

async def ensure_user_thread(
    user: str,
    *,
    parent_channel: discord.TextChannel,
    create: bool = True
) -> discord.abc.Messageable | None:
    """
    Ensure a thread exists (and is recorded) for `user`.

    Returns:
      * Thread channel object if user included and thread exists/found.
      * Parent channel if user included but thread doesn't exist and create=False.
      * Parent channel if user included but we failed to create/fetch the thread.
      * None if user is not included (do nothing).

    Thread type: public thread (adjust if you need private).
    """
    async with CONFIG_LOCK:
        entry = CONFIG["users"].get(user)
        if not entry or not entry.get("included", False):
            return None
        thread_id = entry.get("thread_id")

    # Try existing thread
    if thread_id:
        try:
            thread = await bot.fetch_channel(thread_id)
            if isinstance(thread, discord.Thread):
                return thread
        except Exception:
            thread = None

    # Create a new thread if allowed
    if create:
        try:
            thread = await parent_channel.create_thread(
                name=f"User:{user}",
                type=discord.ChannelType.public_thread
            )
        except Exception as e:  # pragma: no cover
            print(f"⚠️  Failed to create thread for {user}: {e}")
            return parent_channel
        # Persist ID
        async with CONFIG_LOCK:
            CONFIG["users"].setdefault(user, {"included": True, "thread_id": None})
            CONFIG["users"][user]["thread_id"] = thread.id
            save_config(CONFIG)
        return thread

    # No creation requested and no existing thread; fall back to parent
    return parent_channel

# --------------------------------------------------------------------------- #
# ── Lifecycle events                                                         #
# --------------------------------------------------------------------------- #

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id {bot.user.id})")
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel is None:
        sys.exit(f"❌  Could not find channel ID {DISCORD_CHANNEL_ID}")
    # Kick off the background EventStreams task
    bot.loop.create_task(stream_worker(channel))


def main():
    try:
        bot.run(DISCORD_TOKEN)
    finally:
        print("Bye!")


if __name__ == "__main__":
    main()
