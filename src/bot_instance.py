import discord
from discord.ext import commands

# Use "!" *and* register slash commands; documentation promotes "/".
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.before_invoke
async def defer_slash_response(ctx: commands.Context):
    """Acknowledge slash commands before callbacks wait on config locks or I/O."""
    if ctx.interaction is not None and not ctx.interaction.response.is_done():
        await ctx.defer()
