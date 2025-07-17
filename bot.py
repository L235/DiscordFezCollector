#!/usr/bin/env python3
"""
fez_collector — Discord edition
--------------------------------
* Replaces IRC output with Discord messages.
* Reads **and** persists state (config) to a JSON file whose location is
  supplied in the `FEZ_COLLECTOR_STATE` environment variable.
* Lets authorised users update the config live from Discord commands.
* **v1 config schema (per‑user threads):**

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
  the thread mapping for later reuse. The legacy "exclude" concept is removed.
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
# ── Config management / migration                                            #
# --------------------------------------------------------------------------- #

# v1 schema
# {"users": {"Username": {"included": bool, "thread_id": int|None}}}
DEFAULT_CONFIG = {"users": {}}


def _migrate_legacy(cfg: dict) -> dict:
    """
    Upgrade legacy configs that used userIncludeList/userExcludeList.
    Excluded users become included=False; included users=True.
    Thread IDs cannot be recovered retroactively; set None.
    """
    if "users" in cfg:
        # Already new schema
        return cfg

    users_cfg = {}
    inc = cfg.get("userIncludeList", []) or []
    exc = cfg.get("userExcludeList", []) or []
    for u in set(inc) | set(exc):
        users_cfg[u] = {"included": (u in inc), "thread_id": None}

    migrated = {"users": users_cfg}
    return migrated


def load_config() -> dict:
    if not STATE_FILE.exists():
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    with STATE_FILE.open(encoding="utf-8") as fp:
        raw = json.load(fp)
    cfg = _migrate_legacy(raw)
    # Persist migration if needed
    if cfg != raw:
        save_config(cfg)
    return cfg


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
        """
        Return the Discord thread (or fallback channel) for a user.
        Creates the thread if needed & permitted.
        """
        async with CONFIG_LOCK:
            user_entry = CONFIG["users"].get(user)
            # Only post if user exists and is marked included
            if not user_entry or not user_entry.get("included", False):
                return None
            thread_id = user_entry.get("thread_id")

        # Try to fetch existing thread if we have an ID
        thread_chan = None
        if thread_id:
            try:
                thread_chan = await bot.fetch_channel(thread_id)
            except Exception:
                thread_chan = None

        # Create a new thread if missing
        if thread_chan is None:
            try:
                # XXX choose public vs. private; using public for visibility
                thread_chan = await channel.create_thread(
                    name=f"fez-{user}",
                    type=discord.ChannelType.public_thread
                )
            except Exception as e:  # pragma: no cover
                print(f"⚠️  Failed to create thread for {user}: {e}")
                return channel  # fallback to parent
            # Persist the new thread ID
            async with CONFIG_LOCK:
                CONFIG["users"].setdefault(user, {"included": True, "thread_id": None})
                CONFIG["users"][user]["thread_id"] = thread_chan.id
                save_config(CONFIG)

        return thread_chan

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
    When including: create thread lazily (first event) to avoid extra perms churn;
    we *can* eagerly create here if desired (set eager=True below).
    """
    eager_create_thread = False  # flip if you want thread immediately
    async with CONFIG_LOCK:
        entry = CONFIG["users"].get(user)
        if entry is None:
            entry = {"included": included, "thread_id": None}
            CONFIG["users"][user] = entry
            action = "created & set"
        else:
            prev = entry.get("included", False)
            if prev == included:
                await ctx.reply(f"`{user}` already {'included' if included else 'unincluded'}.")
                return
            entry["included"] = included
            action = "updated"
        save_config(CONFIG)

    # Optionally create thread immediately on include
    if included and eager_create_thread:
        parent_channel = ctx.guild.get_channel(DISCORD_CHANNEL_ID)
        if parent_channel is None:
            parent_channel = await bot.fetch_channel(DISCORD_CHANNEL_ID)
        # Reuse stream_worker helper by inlining minimal logic
        try:
            thread_chan = await parent_channel.create_thread(
                name=f"fez-{user}",
                type=discord.ChannelType.public_thread
            )
            async with CONFIG_LOCK:
                CONFIG["users"][user]["thread_id"] = thread_chan.id
                save_config(CONFIG)
        except Exception as e:  # pragma: no cover
            await ctx.reply(f"⚠️  Included `{user}` but could not create thread: {e}")
            return

    await ctx.reply(f"`{user}` {action} to {'included' if included else 'unincluded'} and saved ✓")


@bot.command(name="include")
@commands.check(authorised)
async def include_cmd(ctx: commands.Context, *, user: str):
    """!include <Username> → start tracking a user (creates/uses dedicated thread)."""
    await _set_user_included(ctx, user, True)


@bot.command(name="uninclude")
@commands.check(authorised)
async def uninclude_cmd(ctx: commands.Context, *, user: str):
    """!uninclude <Username> → stop tracking a user (thread retained)."""
    await _set_user_included(ctx, user, False)


@bot.command(name="showconfig")
@commands.check(authorised)
async def showconfig_cmd(ctx: commands.Context):
    """Dump the current config."""
    async with CONFIG_LOCK:
        pretty = json.dumps(CONFIG, indent=2)
    await ctx.reply(f"```json\n{pretty}\n```")

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
