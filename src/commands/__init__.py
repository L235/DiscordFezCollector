"""
Discord command handlers, split by domain.

Importing this package registers all commands with the bot.
"""
from src.commands import (
    thread_commands,
    config_commands,
    filter_commands,
    receiver_commands,
    help_commands,
)
