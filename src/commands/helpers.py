import json
import io
from enum import Enum
from typing import Any, Optional

import discord
from discord.ext import commands

from src.logging_setup import logger
from src.bot_instance import bot
from src.constants import JSON_INDENT, UTF8_ENCODING
from src.config import (
    CONFIG,
    CONFIG_LOCK,
    DISCORD_CHANNEL_IDS,
    OWNER_ID,
    save_config,
    ensure_custom_thread_entry,
    _normalise_list_val,
    mutate_entity_config_list,
)


def authorised(ctx) -> bool:
    """Gatekeeper: only allow the bot owner."""
    return ctx.author.id == OWNER_ID


# Alias kept for use with @commands.check
is_bot_owner = authorised


def _in_parent_channel(ctx: commands.Context) -> bool:
    """True when in any configured parent channel."""
    return ctx.channel.id in DISCORD_CHANNEL_IDS


def _in_custom_thread(ctx: commands.Context) -> bool:
    """True when inside any thread spawned from a parent channel."""
    return (
        isinstance(ctx.channel, discord.Thread)
        and ctx.channel.parent_id in DISCORD_CHANNEL_IDS
    )


def _parse_json_arg(s: str) -> Any:
    try:
        return json.loads(s)
    except Exception:
        return s


def _deepcopy_cfg(obj: Any) -> Any:
    """JSON round-trip copy."""
    try:
        return json.loads(json.dumps(obj))
    except Exception:
        return obj


async def _require_custom_thread(ctx: commands.Context) -> Optional[dict]:
    if not _in_custom_thread(ctx):
        await ctx.reply("This command must be used inside a thread.")
        return None
    return await ensure_custom_thread_entry(str(ctx.channel.id), ctx.channel.name, create_if_missing=True)


class FilterType(Enum):
    PAGE = "page"
    USER = "user"
    SUMMARY = "summary"


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

# Action to (key_map, mutation_kwargs_key) mapping
_ACTION_MAP = {
    "add_include": (_INCLUDE_KEY_MAP, "add"),
    "remove_include": (_INCLUDE_KEY_MAP, "remove"),
    "add_exclude": (_EXCLUDE_KEY_MAP, "add"),
    "remove_exclude": (_EXCLUDE_KEY_MAP, "remove"),
}

_ACTION_LABELS = {
    "add_include": "Now tracking",
    "remove_include": "Removed from tracking",
    "add_exclude": "Now ignoring",
    "remove_exclude": "Removed from ignore list",
}


async def mutate_filter(
    ctx: commands.Context,
    entity_type: str,
    entity_id: str,
    target: FilterType,
    value: str,
    *,
    action: str,
    entity_label: str = "",
) -> None:
    """
    Single function handling all track/ignore/untrack/unignore for any entity type.
    """
    key_map, kwarg = _ACTION_MAP[action]
    config_key = key_map[target]
    vals = _normalise_list_val(value)

    ok, new_list = await mutate_entity_config_list(
        entity_type, entity_id, config_key, **{kwarg: vals}
    )
    label = _ACTION_LABELS[action]
    prefix = f"{entity_label} " if entity_label else ""
    if ok:
        await ctx.reply(f"{prefix}{label} {target.value}: `{vals}`\n`{config_key}`: `{new_list}`")
    else:
        await ctx.reply(f"{prefix}Failed to update {target.value}.")
