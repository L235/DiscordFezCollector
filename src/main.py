import asyncio
import signal
import sys
import discord
from discord.ext import commands

from src.logging_setup import logger
from src.config import DISCORD_TOKEN, DISCORD_CHANNEL_ID
from src.bot_instance import bot
# Import commands to ensure they are registered
import src.commands 
from src.event_streams import stream_worker, eventstream_health_monitor, THREAD_CACHE

# --------------------------------------------------------------------------- #
# ── Lifecycle events                                                         #
# --------------------------------------------------------------------------- #

@bot.event
async def on_error(event, *args, **kwargs):
    """Log any errors that occur in Discord events."""
    logger.error(f"Error in event {event}: {args} {kwargs}")

@bot.event
async def on_command_error(ctx, error):
    """Log command errors."""
    if isinstance(error, commands.CheckFailure):
        logger.warning(f"Unauthorized command attempt by {ctx.author} in {ctx.channel}: {ctx.message.content}")
    elif isinstance(error, commands.CommandNotFound):
        logger.debug(f"Unknown command by {ctx.author}: {ctx.message.content}")
    else:
        logger.error(f"Command error from {ctx.author} in {ctx.channel}: {error}")

@bot.event
async def on_thread_update(before: discord.Thread, after: discord.Thread):
    THREAD_CACHE[after.id] = after

@bot.event
async def on_thread_delete(thread: discord.Thread):
    THREAD_CACHE.pop(thread.id, None)

_stream_task: asyncio.Task = None
_health_monitor_task: asyncio.Task = None

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (id {bot.user.id})")
    # Register (or update) global application commands with Discord.
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} application command(s)")
    except Exception as e:
        logger.warning(f"Failed to sync application commands: {e}")

    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel is None:
        logger.error(f"Could not find channel ID {DISCORD_CHANNEL_ID}")
        # We can't exit here effectively because on_ready might fire multiple times, 
        # but for the first run it's critical.
        # sys.exit(f"Could not find channel ID {DISCORD_CHANNEL_ID}")
        return
        
    logger.info(f"Found target channel: {channel.name} ({channel.id})")
    
    # Start the background EventStreams task and health monitor once.
    global _stream_task, _health_monitor_task
    
    if _stream_task is None or _stream_task.done():
        logger.info("Starting EventStreams worker task")
        _stream_task = bot.loop.create_task(stream_worker(channel))
    else:
        logger.info("EventStreams worker already running; not starting another one")
    
    if _health_monitor_task is None or _health_monitor_task.done():
        logger.info("Starting EventStream health monitor task")
        _health_monitor_task = bot.loop.create_task(eventstream_health_monitor())
    else:
        logger.info("EventStream health monitor already running")

def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    logger.info(f"Received signal {signum}, shutting down gracefully...")
    sys.exit(0)

def main():
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        logger.info("Starting Discord bot...")
        bot.run(DISCORD_TOKEN)
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down...")
    except Exception as e:
        logger.error(f"Unexpected error in main: {e}")
        raise
    finally:
        logger.info("Bot shutdown complete")

if __name__ == "__main__":
    main()
