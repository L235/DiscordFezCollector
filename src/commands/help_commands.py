from discord.ext import commands

from src.logging_setup import logger
from src.bot_instance import bot
from src.constants import DISCORD_MESSAGE_LIMIT


def _build_help_sections() -> list:
    """
    Build help text from registered bot commands.
    Returns list of strings, each under DISCORD_MESSAGE_LIMIT chars.
    """
    # Gather commands grouped by their parent group (or "Basic" for top-level)
    groups: dict[str, list[str]] = {}

    for cmd in sorted(bot.commands, key=lambda c: c.qualified_name):
        if cmd.hidden:
            continue

        if isinstance(cmd, commands.Group):
            # Add the group's subcommands
            group_name = cmd.qualified_name
            for subcmd in sorted(cmd.commands, key=lambda c: c.qualified_name):
                if subcmd.hidden:
                    continue
                desc = subcmd.description or subcmd.short_doc or ""
                line = f"* `/{subcmd.qualified_name}` - {desc}"
                groups.setdefault(group_name, []).append(line)
        else:
            desc = cmd.description or cmd.short_doc or ""
            line = f"* `/{cmd.qualified_name}` - {desc}"
            groups.setdefault("Basic Commands", []).append(line)

    # Build sections
    sections = []
    for group_name, lines in groups.items():
        header = f"**{group_name.title() if group_name != 'Basic Commands' else group_name}:**"
        sections.append(header + "\n" + "\n".join(lines))

    # Chunk into messages under Discord limit
    chunks = []
    current = "**Available Commands:**\n\n"
    for section in sections:
        candidate = current + section + "\n\n"
        if len(candidate) > DISCORD_MESSAGE_LIMIT:
            if current.strip():
                chunks.append(current.strip())
            current = section + "\n\n"
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())

    return chunks


@bot.hybrid_command(name="fezhelp",
                    description="Show available commands",
                    with_app_command=True)
async def fezhelp_cmd(ctx: commands.Context):
    """Display available commands and their descriptions."""
    logger.info(f"Help command from {ctx.author} in {ctx.channel}")
    chunks = _build_help_sections()
    for i, chunk in enumerate(chunks):
        if i == 0:
            await ctx.reply(chunk)
        else:
            await ctx.send(chunk)
