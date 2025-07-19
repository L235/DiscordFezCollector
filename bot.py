#!/usr/bin/env python3
"""
fez_collector - Discord edition
--------------------------------
* Monitors MediaWiki EventStreams and posts changes to Discord.
* Reads **and** persists state (config) to a JSON file whose location is
  supplied in the `FEZ_COLLECTOR_STATE` environment variable.
* Lets authorised users update the config live from Discord **slash**
  commands ( `/...` ).  Legacy **bang** prefixes (`!...`) are retained for
  backward compatibility.

**Environment Variables:**
* `FEZ_COLLECTOR_DISCORD_TOKEN` - Discord bot token (required)
* `FEZ_COLLECTOR_CHANNEL_ID`   - Parent channel ID for threads (required)
* `FEZ_COLLECTOR_STATE`        - Path to config JSON file (default: "./state/config.json")

**Available Commands (preferred → legacy):**
* `/ping`  (`!ping`) - Test bot responsiveness
* `/add <Username>`  (`!add`) - Create a "User:&lt;Username&gt;" thread **and** add the user to `userIncludeList`
* `/addcustom <name>`  (`!addcustom`) - Create a generic filter thread (parent channel only)
* `/globalconfig [get]`  (replaces `!showconfig`) - Download full configuration as a JSON attachment
* `/globalconfig set` - **Replace** the entire configuration from an attached JSON file (**dangerous**)
* `/activate` / `/deactivate` - Toggle activity for the *current thread*
* `/config [get]` - Show current thread configuration
* `/config set <key> <json>` - Set configuration value
* `/config add|remove|clear ...` - Mutate list‑type configuration fields

**Config Schema (v0.8):**

      {
          "version": "0.8",
          "threads": {
              "<thread_id>": {
                  "name": "User:Username",       # display only
                  "active": true,
                  "config": {
                      "siteName": "",            # '' => any; e.g. "en.wikipedia.org"
                      "pageIncludePatterns": [],
                      "pageExcludePatterns": [],
                      "userExcludeList": [],
                      "userIncludeList": ["Username"],
                      "summaryIncludePatterns": [],
                      "summaryExcludePatterns": []
                  }
              }
          }
      }
"""
VERSION = "0.8-discord-harmonised"
print(f"fez_collector {VERSION} initialising...")

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
import re, io
from re import RegexFlag, compile, search
from typing import Any, List, Optional, Tuple

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



DEFAULT_CUSTOM_CONFIG = {
    "siteName": "",
    "pageIncludePatterns": [],
    "pageExcludePatterns": [],
    "userExcludeList": [],
    "userIncludeList": [],
    "summaryIncludePatterns": [],
    "summaryExcludePatterns": [],
}

# Config schema (see docstring):
DEFAULT_CONFIG = {"version": "0.8", "threads": {}}


def load_config() -> dict:
    if not STATE_FILE.exists():
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    with STATE_FILE.open(encoding="utf-8") as fp:
        raw = json.load(fp)
    # ensure key exists - older configs will silently keep working,
    # but we no longer mutate them in-place.
    raw.setdefault("threads", {})
    return raw


def save_config(cfg: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(cfg, fp, indent=2, sort_keys=True)
    tmp.replace(STATE_FILE)


CONFIG = load_config()
CONFIG_LOCK = asyncio.Lock()  # to prevent simultaneous writes

# --------------------------------------------------------------------------- #
# ── Custom thread helpers                                                    #
# --------------------------------------------------------------------------- #

def _event_ts_to_epoch(change: dict) -> Optional[float]:
    """
    Best-effort extraction of an event timestamp (UTC seconds).

    MediaWiki recentchange events *usually* include an integer `timestamp`
    (Unix epoch). Some other stream payloads (or schema bumps / edge cases)
    may omit it; in that case fall back to `meta.dt` (ISO 8601).

    Returns float seconds, or None if unavailable/unparseable.
    """
    ts = change.get("timestamp")
    if isinstance(ts, (int, float)):
        return float(ts)
    # Occasionally comes as str
    if isinstance(ts, str) and ts.isdigit():
        return float(ts)
    meta = change.get("meta") or {}
    dt_s = meta.get("dt")
    if dt_s:
        try:
            return datetime.fromisoformat(dt_s.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
    return None


def _blank_custom_cfg() -> dict:
    return json.loads(json.dumps(DEFAULT_CUSTOM_CONFIG))  # deep copy


async def ensure_custom_thread_entry(thread: discord.Thread, *, create_if_missing: bool = False) -> Optional[dict]:
    """
    Ensure CONFIG entry exists for given discord.Thread. Returns entry (dict) or None.
    """
    tid = str(thread.id)
    # Every thread (incl. "User:" ones) is now a first-class custom thread.

    async with CONFIG_LOCK:
        entry = CONFIG["threads"].get(tid)
        if entry is None and create_if_missing:
            entry = {
                "name": thread.name,
                "active": True,
                "config": _blank_custom_cfg(),
            }
            CONFIG["threads"][tid] = entry
            save_config(CONFIG)
        return entry


async def set_custom_thread_active(thread_id: int, active: bool) -> bool:
    async with CONFIG_LOCK:
        entry = CONFIG["threads"].get(str(thread_id))
        if not entry:
            return False
        entry["active"] = active
        save_config(CONFIG)
        return True


async def update_custom_thread_config(thread_id: int, new_cfg: dict) -> bool:
    async with CONFIG_LOCK:
        entry = CONFIG["threads"].get(str(thread_id))
        if not entry:
            return False
        entry["config"] = new_cfg
        save_config(CONFIG)
        return True


def _normalise_list_val(v: Any) -> List[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)]


async def mutate_custom_thread_config_list(thread_id: int, key: str, *, add: Optional[List[str]] = None,
                                           remove: Optional[List[str]] = None, clear: bool = False) -> Tuple[bool, Optional[List[str]]]:
    """
    Mutate an array field of a custom thread config. Returns (ok, new_list|None).
    """
    async with CONFIG_LOCK:
        entry = CONFIG["threads"].get(str(thread_id))
        if not entry:
            return False, None
        cfg = entry["config"]
        if key not in cfg:
            # create list field
            cfg[key] = []
        if clear:
            cfg[key] = []
        else:
            lst = list(cfg[key])
            if add:
                for it in add:
                    if it not in lst:
                        lst.append(it)
            if remove:
                lst = [it for it in lst if it not in remove]
            cfg[key] = lst
        save_config(CONFIG)
        return True, list(cfg[key])


def _compile_pattern_list(patterns: List[str]) -> Optional[re.Pattern]:
    """
    Accept list-ish input; coerce scalars to a single-element list.
    """
    if isinstance(patterns, str):
        patterns = [patterns]
    elif not isinstance(patterns, list):
        try:
            patterns = list(patterns)
        except Exception:
            patterns = [str(patterns)]
    patterns = [str(p) for p in patterns if p]
    if not patterns:
        return None
    return compile(f"({'|'.join(patterns)})", RegexFlag.IGNORECASE)


class CustomFilter:
    __slots__ = (
        "site_name",
        "page_include",
        "page_exclude",
        "sum_include",
        "sum_exclude",
        "user_include",
        "user_exclude",
    )

    def __init__(self, cfg: dict):
        self.site_name   = cfg.get("siteName", "") or ""
        self.page_include = _compile_pattern_list(cfg.get("pageIncludePatterns", []))
        self.page_exclude = _compile_pattern_list(cfg.get("pageExcludePatterns", []))
        self.sum_include  = _compile_pattern_list(cfg.get("summaryIncludePatterns", []))
        self.sum_exclude  = _compile_pattern_list(cfg.get("summaryExcludePatterns", []))
        self.user_include = set(cfg.get("userIncludeList", []))
        self.user_exclude = set(cfg.get("userExcludeList", []))

    def matches(self, change: dict) -> bool:
        user = change["user"]
        # Some EventStreams variants may lack 'title'; guard.
        title = change.get("title", "")
        comment = change.get("log_action_comment") or change.get("comment") or ""
        server = change.get("server_name", "")

        if self.site_name and self.site_name.lower() != server.lower():
            return False
        # If we require a siteName AND title is empty (rare), bail out.
        if self.site_name and not title:
            return False
        if user in self.user_exclude:
            return False
        if self.page_exclude and search(self.page_exclude, title):
            return False
        if self.sum_exclude and search(self.sum_exclude, comment):
            return False
        # include
        if user in self.user_include:
            return True
        if self.page_include and search(self.page_include, title):
            return True
        if self.sum_include and search(self.sum_include, comment):
            return True
        return False


# --------------------------------------------------------------------------- #
# ── MediaWiki EventStreams setup                                             #
# --------------------------------------------------------------------------- #

#
# We no longer restrict to a single wiki; collect everything and filter later.
# This will include non-English projects unless custom filters narrow scope.
#
site = Site()  # default site; EventStreams ignores this for global streams
# -------------------------------------------------------------------- #
# EventStreams requires its `since=` parameter to be either a Unix-ms
# epoch or an ISO-8601 timestamp *without* micro-seconds and without a
# literal "+" in the TZ designator (the plus would be decoded as
# whitespace on the server side).  Using "Z" explicitly marks UTC and
# avoids URL‑encoding issues.
# -------------------------------------------------------------------- #

_NOW_UTC_ISO = datetime.utcnow().replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

stream = EventStreams(
    streams=["recentchange", "revision-create"],
    since=_NOW_UTC_ISO
)
# (no register_filter)

# --------------------------------------------------------------------------- #
# ── Discord bot setup                                                        #
# --------------------------------------------------------------------------- #

# Use "!" *and* register slash commands; documentation promotes "/".
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --------------------------------------------------------------------------- #
# ── Message formatting                                                        #
# --------------------------------------------------------------------------- #

def format_change(change: dict) -> str:
    """Turn one EventStreams record into a concise Discord message."""
    user = change['user']
    title = change.get("title", "(no title)")
    comment = change.get("comment") or "(no summary)"
    
    if change["type"] == "log":
        log_comment = change.get("log_action_comment") or comment
        link = f"https://{change.get('server_name','')}/w/index.php?title=Special:Log&logid={change.get('log_id','')}"
        return f"**{user}** {log_comment} \n<{link}>"

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
    """
    Background task: tail MediaWiki EventStreams and route posts to **active
    custom threads**.  (Per-user routing removed in v0.7.)
    """
    loop = asyncio.get_running_loop()

    async def active_custom_filters() -> List[Tuple[int, CustomFilter]]:
        """Return list of (thread_id, CustomFilter) for active custom threads."""
        async with CONFIG_LOCK:
            items = [
                (int(tid), CustomFilter(entry["config"]))
                for tid, entry in CONFIG["threads"].items()
                if entry.get("active", False)
            ]
        return items

    async def get_thread_obj(tid: int) -> Optional[discord.Thread]:
        try:
            ch = await bot.fetch_channel(tid)
        except Exception:
            return None
        return ch if isinstance(ch, discord.Thread) else None

    for change in stream:
        # Let Discord breathe
        await asyncio.sleep(0)  # cooperative yield

        # Robust timestamp handling
        ts = _event_ts_to_epoch(change)
        if ts is None:
            # If we cannot determine freshness, treat as *not stale* but log once.
            # To avoid log spam, gate on an attribute.
            if not getattr(stream_worker, "_warned_missing_ts", False):
                print("⚠️  Event without timestamp encountered; treating as fresh. Further warnings suppressed.")
                setattr(stream_worker, "_warned_missing_ts", True)
            stale = False
        else:
            current_time = datetime.now(timezone.utc).timestamp()
            stale = (current_time - ts) > STALENESS_SECS

        # Some events may lack user (rare for rc, common for revision-create)
        user = change.get("user")
        if stale:
            continue
        if not user:
            # Nothing sensible to route; skip quietly.
            continue

        # Determine routing targets
        targets: List[discord.abc.Messageable] = []

        # Custom threads only
        customs = await active_custom_filters()
        if customs:
            for tid, filt in customs:
                try:
                    if filt.matches(change):
                        th = await get_thread_obj(tid)
                        if th is not None:
                            targets.append(th)
                except Exception as e:  # pragma: no cover
                    print(f"⚠️  custom filter error for {tid}: {e}")

        if not targets:
            continue  # nothing to do

        msg = format_change(change)
        if len(msg) > 2000:  # Discord hard limit
            msg = msg[:1990] + "..."

        # Send to all targets; fire and forget
        for tgt in targets:
            await loop.create_task(tgt.send(msg))

# --------------------------------------------------------------------------- #
# ── Discord commands                                                         #
# --------------------------------------------------------------------------- #

def authorised(ctx) -> bool:
    """Gatekeeper: only allow guild moderators or the bot owner."""
    return ctx.author.guild_permissions.manage_guild or ctx.author.id == bot.owner_id


@bot.hybrid_command(name="ping",
                    description="Test bot responsiveness",
                    with_app_command=True)
async def ping_cmd(ctx: commands.Context):
    await ctx.reply("pong")





@bot.hybrid_command(name="add",
                    description="Create a per-user custom thread and include them",
                    with_app_command=True)
@commands.check(authorised)
async def add_cmd(ctx: commands.Context, *, user: str):
    """
    Convenience wrapper that delegates to the new `/addcustom` logic,
    then appends the supplied username to `userIncludeList`.
    """
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        await ctx.reply("Run `/add` in the parent channel.")
        return

    # create "User:&lt;Username&gt;" thread
    try:
        thread = await ctx.channel.create_thread(
            name=f"User:{user}",
            type=discord.ChannelType.public_thread
        )
    except Exception as e:
        await ctx.reply(f"Failed to create thread: {e}")
        return

    await ensure_custom_thread_entry(thread, create_if_missing=True)
    _, new_list = await mutate_custom_thread_config_list(
        thread.id, "userIncludeList", add=[user]
    )
    await ctx.reply(f"Tracking **{user}** in <#{thread.id}>.\n`userIncludeList`: `{new_list}`")


# --------------------------------------------------------------------------- #
# ── Global configuration ("/globalconfig ...")                                 #
# --------------------------------------------------------------------------- #


@bot.hybrid_group(name="globalconfig",
                  invoke_without_command=True,
                  description="Global configuration operations",
                  with_app_command=True)
@commands.check(authorised)
async def globalconfig_group(ctx: commands.Context):
    """`/globalconfig` (no subcommand) -> `/globalconfig get`."""
    await ctx.invoke(globalconfig_get_cmd)


@globalconfig_group.command(name="get",
                            description="Download full configuration as JSON",
                            with_app_command=True)
@commands.check(authorised)
async def globalconfig_get_cmd(ctx: commands.Context):
    async with CONFIG_LOCK:
        data = json.dumps(CONFIG, indent=2).encode("utf-8")
    file = discord.File(io.BytesIO(data), filename="global_config.json")
    await ctx.reply(file=file, mention_author=False)


@globalconfig_group.command(name="set",
                            description="Replace configuration from attached JSON (DANGEROUS)",
                            with_app_command=True)
@commands.check(authorised)
async def globalconfig_set_cmd(ctx: commands.Context):
    if not ctx.message.attachments:
        await ctx.reply("Attach a **JSON** file to `/globalconfig set`.", mention_author=False)
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





# --------------------------------------------------------------------------- #
# ── Custom thread commands                                                   #
# --------------------------------------------------------------------------- #

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


@bot.hybrid_command(name="addcustom",
                    description="Create a custom-filter thread (parent channel only)",
                    with_app_command=True)
@commands.check(authorised)
async def addcustom_cmd(ctx: commands.Context, *, threadname: str = ""):
    """/addcustom <threadname> → create a custom filter thread (in parent channel only)."""
    if not _in_parent_channel(ctx):
        await ctx.reply("Use `/addcustom` in the parent channel.")
        return
    if not threadname.strip():
        await ctx.reply("Please provide a thread name: `/addcustom <name>`.")
        return
    try:
        thread = await ctx.channel.create_thread(
            name=threadname,
            type=discord.ChannelType.public_thread
        )
    except Exception as e:  # pragma: no cover
        await ctx.reply(f"Failed to create thread: {e}")
        return
    await ensure_custom_thread_entry(thread, create_if_missing=True)
    await ctx.reply(f"Thread created: <#{thread.id}> (active). Configure with `/config` inside the thread.")


async def _require_custom_thread(ctx: commands.Context) -> Optional[dict]:
    if not _in_custom_thread(ctx):
        await ctx.reply("This command must be used inside a thread.")
        return None
    entry = await ensure_custom_thread_entry(ctx.channel, create_if_missing=False)
    if entry is None:
        # thread exists but no entry (perhaps race); create one
        entry = await ensure_custom_thread_entry(ctx.channel, create_if_missing=True)
    return entry


@bot.hybrid_command(name="activate",
                    description="Activate current custom thread",
                    with_app_command=True)
@commands.check(authorised)
async def activate_custom_thread_cmd(ctx: commands.Context):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    ok = await set_custom_thread_active(ctx.channel.id, True)
    if ok:
        await ctx.reply("Activated.")
    else:
        await ctx.reply("Failed to activate (missing config?).")


@bot.hybrid_command(name="deactivate",
                    description="Deactivate current custom thread",
                    with_app_command=True)
@commands.check(authorised)
async def deactivate_custom_thread_cmd(ctx: commands.Context):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    ok = await set_custom_thread_active(ctx.channel.id, False)
    if ok:
        await ctx.reply("Deactivated.")
    else:
        await ctx.reply("Failed to deactivate (missing config?).")


@bot.hybrid_group(name="config",
                  invoke_without_command=True,
                  description="Thread configuration operations",
                  with_app_command=True)
@commands.check(authorised)
async def config_group(ctx: commands.Context):
    """`/config` (no subcommand) -> `/config get`."""
    await ctx.invoke(config_get_cmd)


@config_group.command(name="get",
                      description="Show current thread configuration",
                      with_app_command=True)
@commands.check(authorised)
async def config_get_cmd(ctx: commands.Context):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    pretty = json.dumps(entry["config"], indent=2)
    await ctx.reply(f"```json\n{pretty}\n```")


@config_group.command(name="set",
                      description="Set a configuration key",
                      with_app_command=True)
@commands.check(authorised)
async def config_set_cmd(ctx: commands.Context, key: str, *, value: str):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    parsed = _parse_json_arg(value)
    # If key unknown, set anyway
    cfg = _deepcopy_cfg(entry["config"])
    cfg[key] = parsed
    ok = await update_custom_thread_config(ctx.channel.id, cfg)
    if ok:
        await ctx.reply(f"Set `{key}`: `{parsed}`")
    else:
        await ctx.reply("Failed to set.")


@config_group.command(name="add",
                      description="Append value(s) to a list key",
                      with_app_command=True)
@commands.check(authorised)
async def config_add_cmd(ctx: commands.Context, key: str, *, value: str):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    vals = _normalise_list_val(_parse_json_arg(value))
    ok, new_list = await mutate_custom_thread_config_list(ctx.channel.id, key, add=vals)
    if ok:
        await ctx.reply(f"Added to `{key}`: `{vals}`\nNow: `{new_list}`")
    else:
        await ctx.reply("Failed to add.")


@config_group.command(name="remove",
                      description="Remove value(s) from a list key",
                      with_app_command=True)
@commands.check(authorised)
async def config_remove_cmd(ctx: commands.Context, key: str, *, value: str):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    vals = _normalise_list_val(_parse_json_arg(value))
    ok, new_list = await mutate_custom_thread_config_list(ctx.channel.id, key, remove=vals)
    if ok:
        await ctx.reply(f"Removed from `{key}`: `{vals}`\nNow: `{new_list}`")
    else:
        await ctx.reply("Failed to remove.")


@config_group.command(name="clear",
                      description="Clear a list key",
                      with_app_command=True)
@commands.check(authorised)
async def config_clear_cmd(ctx: commands.Context, key: str):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    ok, _ = await mutate_custom_thread_config_list(ctx.channel.id, key, clear=True)
    if ok:
        await ctx.reply(f"Cleared `{key}`.")
    else:
        await ctx.reply("Failed to clear.")


# (legacy per-user thread helpers deleted)

# --------------------------------------------------------------------------- #
# ── Lifecycle events                                                         #
# --------------------------------------------------------------------------- #

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id {bot.user.id})")
    # Register (or update) global application commands with Discord.
    try:
        synced = await bot.tree.sync()
        print(f"✅  Synced {len(synced)} application command(s)")
    except Exception as e:
        print(f"⚠️  Failed to sync application commands: {e}")

    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    # Preload custom thread entries for existing threads (best effort)
    async with CONFIG_LOCK:
        # nothing to do here; entries created on demand
        pass

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
