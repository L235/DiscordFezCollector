import asyncio
import discord
from typing import Dict, Optional

from src.logging_setup import logger
from src.models import DiscordRetryConfig
from src.bot_instance import bot

class WebhookError(Exception):
    """Raised when a webhook send fails permanently."""

async def discord_api_call_with_backoff(coro_func, *args, **kwargs):
    """
    Execute a Discord API coroutine with exponential backoff on server errors.

    discord.py already handles 429 rate limits internally (with bucket tracking,
    global rate limits, and Cloudflare ban detection). This wrapper adds retry
    logic for 5xx server errors — notably 503, which discord.py does not retry
    on the bot API path (http.py). See Pycord PR #2395, discordgo PR #1586.

    Args:
        coro_func: An async callable (not an awaitable) that makes a Discord API call
        *args, **kwargs: Arguments to pass to coro_func

    Returns:
        The result of the Discord API call

    Raises:
        discord.NotFound: If the resource was not found (404)
        discord.Forbidden: If access is denied (403)
        discord.HTTPException: If max attempts exceeded or other HTTP error
    """
    backoff = DiscordRetryConfig.INITIAL_BACKOFF_SECS
    last_exception = None

    for attempt in range(DiscordRetryConfig.MAX_ATTEMPTS):
        try:
            return await coro_func(*args, **kwargs)
        except (discord.NotFound, discord.Forbidden):
            raise
        except discord.HTTPException as e:
            last_exception = e
            if e.status >= 500:
                logger.warning(
                    f"Discord server error ({e.status}). Attempt {attempt + 1}/{DiscordRetryConfig.MAX_ATTEMPTS}. "
                    f"Waiting {backoff:.1f}s before retry..."
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, DiscordRetryConfig.MAX_BACKOFF_SECS)
            else:
                raise

    # Max attempts exceeded
    logger.error(
        f"Discord API call failed after {DiscordRetryConfig.MAX_ATTEMPTS} attempts. "
        f"Last error: {last_exception}"
    )
    if last_exception:
        raise last_exception
    raise discord.HTTPException(response=None, message="Max attempts reached with no specific error")


async def send_message_with_backoff(target: discord.abc.Messageable, content: str) -> Optional[discord.Message]:
    """
    Send a message to a Discord channel/thread with server error retry handling.

    Args:
        target: The channel or thread to send to
        content: The message content

    Returns:
        The sent Message object, or None if sending failed
    """
    try:
        return await discord_api_call_with_backoff(target.send, content)
    except discord.HTTPException as e:
        logger.error(f"Failed to send message to {getattr(target, 'name', target)}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error sending message to {getattr(target, 'name', target)}: {e}")
        return None

# Cache for webhook objects: url -> discord.Webhook
_WEBHOOK_CACHE: Dict[str, discord.Webhook] = {}

async def send_webhook_with_backoff(
    webhook_url: str,
    content: str,
    receiver_key: str
) -> bool:
    """
    Send a message to a Discord webhook with exponential backoff on server errors.
    Uses discord.py's native webhook support.

    Args:
        webhook_url: The Discord webhook URL
        content: The message content
        receiver_key: Key identifying this receiver (for logging)

    Returns:
        True if message was sent successfully

    Raises:
        WebhookError: If the webhook is permanently broken (404, 401, etc.)
    """
    # Get or create webhook object (discord.py handles the session internally)
    webhook = _WEBHOOK_CACHE.get(webhook_url)
    if webhook is None:
        try:
            webhook = discord.Webhook.from_url(webhook_url, client=bot)
            _WEBHOOK_CACHE[webhook_url] = webhook
        except Exception as e:
            logger.error(f"Invalid webhook URL for '{receiver_key}': {e}")
            raise WebhookError(f"Invalid webhook URL: {e}")

    try:
        # Use the existing backoff helper - it handles 5xx retries
        await discord_api_call_with_backoff(webhook.send, content)
        return True
    except discord.NotFound:
        # Webhook was deleted
        _WEBHOOK_CACHE.pop(webhook_url, None)
        logger.error(f"Webhook not found for '{receiver_key}' (deleted?)")
        raise WebhookError("Webhook not found (404)")
    except discord.Forbidden:
        # Permission denied
        _WEBHOOK_CACHE.pop(webhook_url, None)
        logger.error(f"Webhook forbidden for '{receiver_key}'")
        raise WebhookError("Webhook forbidden (403)")
    except discord.HTTPException as e:
        logger.error(f"Webhook HTTP error for '{receiver_key}': {e}")
        raise WebhookError(f"HTTP error: {e}")
