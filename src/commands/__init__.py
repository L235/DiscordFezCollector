"""
Discord command handlers, split by domain.

Importing this package registers all commands with the bot.
"""
from src.commands import (
    config_commands,
    filter_commands,
    help_commands,
    receiver_commands,
    thread_commands,
)
