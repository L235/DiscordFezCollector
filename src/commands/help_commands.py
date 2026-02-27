from discord.ext import commands

from src.logging_setup import logger
from src.bot_instance import bot


@bot.hybrid_command(name="fezhelp",
                    description="Show available commands",
                    with_app_command=True)
async def fezhelp_cmd(ctx: commands.Context):
    """Display available commands. Auto-generated in Task 6."""
    logger.info(f"Help command from {ctx.author} in {ctx.channel}")

    chunks = [
        """**Available Commands:**

**Basic Commands:**
* `/fezhelp` - Show this help message
* `/status` - Show current thread configuration

**Thread Management (run in parent channel):**
* `/new thread <name>` - Create a new tracking thread
* `/new userthread <Username>` - Create a "User:Username" thread and auto-add to userIncludeList
* `/activate` - Activate current thread
* `/deactivate` - Deactivate current thread""",

        """**Tracking Shortcuts (run inside a thread):**
* `/track page|user|summary <value>` - Add to include filters
* `/ignore page|user|summary <value>` - Add to exclude filters
* `/untrack page|user|summary <value>` - Remove from include filters
* `/unignore page|user|summary <value>` - Remove from exclude filters

**Global Configuration:**
* `/globalconfig getraw` - Download full configuration as JSON
* `/globalconfig setraw [attachment]` - Replace configuration from attached JSON file (DANGEROUS)""",

        """**Advanced Thread Configuration:**
* `/config` - Show current thread configuration (same as `/status`)
* `/config set <key> <json>` - Set configuration value
* `/config add <key> <value>` - Add to list configuration
* `/config remove <key> <value>` - Remove from list configuration
* `/config clear <key>` - Clear list configuration
* `/config getraw` - Download raw JSON for current thread
* `/config setraw [attachment]` - Replace current-thread configuration from attached JSON (DANGEROUS)""",

        """**Receiver Commands (owner only):**
* `/receiver list` - List all receivers
* `/receiver add <key> <name>` - Create a new receiver
* `/receiver remove <key>` - Delete a receiver
* `/receiver activate <key>` / `/receiver deactivate <key>` - Toggle receiver
* `/receiver config <key>` - Show receiver configuration
* `/receiver track|ignore|untrack|unignore <key> page|user|summary <value>` - Manage filters"""
    ]

    for i, chunk in enumerate(chunks):
        if i == 0:
            await ctx.reply(chunk)
        else:
            await ctx.send(chunk)
