#!/usr/bin/env python3
"""
fez_collector — Discord edition
--------------------------------
* Replaces IRC output with Discord messages.
* Reads **and** persists state (config) to a JSON file whose location is
  supplied in the `FEZ_COLLECTOR_STATE` environment variable.
* Lets authorised users update the config live from Discord commands.
* Config now tracks **only** the users we care about:
      {
          "userIncludeList": [],
          "userExcludeList": []
      }
"""
VERSION = "0.4-discord"
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

DEFAULT_CONFIG = {"userIncludeList": [], "userExcludeList": []}


def load_config() -> dict:
    if not STATE_FILE.exists():
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    with STATE_FILE.open(encoding="utf-8") as fp:
        return json.load(fp)


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
    """Background task: tail MediaWiki EventStreams and post to Discord."""
    loop = asyncio.get_running_loop()

    def should_post(user: str) -> bool:
        inc = CONFIG["userIncludeList"]
        exc = CONFIG["userExcludeList"]
        if user in exc:
            return False
        if inc:                       # if include-list is non-empty, whitelist it
            return user in inc
        return True                   # otherwise allow anything not excluded

    for change in stream:
        # Let Discord breathe
        await asyncio.sleep(0)  # cooperative yield

        ts     = float(change["timestamp"])
        current_time = datetime.now(timezone.utc).timestamp()
        stale  = (current_time - ts) > STALENESS_SECS
        if stale or not should_post(change["user"]):
            continue

        msg = format_change(change)
        if len(msg) > 2000:           # Discord hard limit
            msg = msg[:1990] + "…"
        await loop.create_task(channel.send(msg))

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


async def _update_list(ctx, list_name: str, user: str, add: bool):
    async with CONFIG_LOCK:
        lst = CONFIG[list_name]
        if add and user not in lst:
            lst.append(user)
            action = "added to"
        elif not add and user in lst:
            lst.remove(user)
            action = "removed from"
        else:
            await ctx.reply(f"`{user}` already {'in' if add else 'not in'} `{list_name}`")
            return
        save_config(CONFIG)
    await ctx.reply(f"`{user}` {action} `{list_name}` and saved ✓")


@bot.command(name="include")
@commands.check(authorised)
async def include_cmd(ctx: commands.Context, *, user: str):
    """!include <Username> → whitelist a user."""
    await _update_list(ctx, "userIncludeList", user, add=True)


@bot.command(name="uninclude")
@commands.check(authorised)
async def uninclude_cmd(ctx: commands.Context, *, user: str):
    await _update_list(ctx, "userIncludeList", user, add=False)


@bot.command(name="exclude")
@commands.check(authorised)
async def exclude_cmd(ctx: commands.Context, *, user: str):
    """!exclude <Username> → block a user."""
    await _update_list(ctx, "userExcludeList", user, add=True)


@bot.command(name="unexclude")
@commands.check(authorised)
async def unexclude_cmd(ctx: commands.Context, *, user: str):
    await _update_list(ctx, "userExcludeList", user, add=False)


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
