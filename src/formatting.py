"""Message formatting: wiki events → Discord messages, and operator notices.

Everything here is pure string building; no Discord or network access.
"""


def format_change(change: dict, *, link_style: str = "title") -> str:
    """Turn one EventStreams record into a concise Discord message.

    Args:
        change: The EventStreams change record.
        link_style: "title" to link the page title (default),
                    "action" to link the action verb instead.
    """
    user = change['user']
    title = change.get("title", "(no title)")
    comment = change.get("comment") or "(no summary)"

    if change["type"] == "log":
        log_comment = change.get("log_action_comment") or comment
        link = f"https://{change.get('server_name','')}/w/index.php?title=Special:Log&logid={change.get('log_id','')}"

        # Better linking for AbuseFilter hits
        if change.get("log_type") == "abusefilter" and change.get("log_action") == "hit":
            log_params = change.get("log_params")
            if isinstance(log_params, dict) and "log" in log_params:
                link = f"https://{change.get('server_name','')}/wiki/Special:AbuseLog/{log_params['log']}"

        return f"**{user}** {log_comment} \n<{link}>"

    # For regular edits
    page_url = f"https://{change['server_name']}/wiki/{title.replace(' ', '_')}"

    # Handle different change types that may or may not have revision info
    if change["type"] == "edit" and "revision" in change:
        diff_url = f"https://{change['server_name']}/w/index.php?diff={change['revision']['new']}"
        if link_style == "action":
            return f"**{user}** [edited](<{diff_url}>) **[{title}](<{page_url}>)** ({comment})"
        return f"**{user}** edited **[{title}](<{page_url}>)** ({comment}) \n<{diff_url}>"
    elif change["type"] == "new":
        if link_style == "action":
            return f"**{user}** [created](<{page_url}>) **{title}** ({comment})"
        return f"**{user}** created **[{title}](<{page_url}>)** ({comment})"
    elif change["type"] == "categorize":
        if link_style == "action":
            return f"**{user}** [categorized](<{page_url}>) **{title}** ({comment})"
        return f"**{user}** categorized **[{title}](<{page_url}>)** ({comment})"
    else:
        # Fallback for other change types
        if link_style == "action":
            return f"**{user}** [modified](<{page_url}>) **{title}** ({comment})"
        return f"**{user}** modified **[{title}](<{page_url}>)** ({comment})"


def format_receiver_disabled_notice(key: str, reason: str) -> str:
    """Build the main-channel notice posted when a receiver is auto-disabled.

    Surfaces the silent failure: which receiver, why, and how to recover — so a
    permanently-broken webhook is noticed instead of dying quietly.
    """
    return (
        f"⚠️ Receiver **{key}** was automatically disabled after a webhook "
        f"failure: {reason}\nReactivate with `/receiver activate {key}` once the "
        f"webhook is fixed."
    )


def format_thread_disabled_notice(thread_id: int, reason: str) -> str:
    """Build the main-channel notice posted when a custom thread is auto-disabled
    because it became permanently unsendable (deleted / no access)."""
    return (
        f"⚠️ Custom thread <#{thread_id}> (`{thread_id}`) was automatically "
        f"deactivated: {reason}\nRecreate the thread (or restore access) to "
        f"resume routing."
    )


def format_startup_notice() -> str:
    """Build the main-channel notice posted once when the bot (re)starts."""
    return "✅ **DiscordFezCollector** started and connected to Wikimedia EventStreams."


def format_eventstream_timeout_notice(seconds: float) -> str:
    """Build the main-channel notice posted before the process restarts itself
    because the Wikimedia stream went silent."""
    return (
        f"⚠️ No Wikimedia events received for {int(seconds)}s — the stream looks "
        f"stalled; restarting the bot to recover."
    )
