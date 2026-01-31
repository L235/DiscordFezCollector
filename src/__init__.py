"""
fez_collector source package.

This package contains the modular components of the Discord Fez Collector bot:
- bot_instance: Discord bot instance creation
- commands: Discord command handlers
- config: Configuration management and persistence
- constants: Application-wide constants
- discord_utils: Discord API helpers with rate limiting
- event_streams: MediaWiki EventStreams processing
- logging_setup: Logging configuration
- main: Application entry point
- models: Data models and filter classes
"""

__all__ = [
    "bot_instance",
    "commands",
    "config",
    "constants",
    "discord_utils",
    "event_streams",
    "logging_setup",
    "main",
    "models",
]
