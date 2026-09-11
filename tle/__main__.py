import argparse
import asyncio
import logging
import os
import discord
from logging.handlers import TimedRotatingFileHandler
from os import environ
from pathlib import Path

import seaborn as sns
from discord.ext import commands
from matplotlib import pyplot as plt

from tle import constants
from tle.util import codeforces_common as cf_common
from tle.util import discord_common, font_downloader



def setup():
    # Make required directories.
    for path in constants.ALL_DIRS:
        os.makedirs(path, exist_ok=True)

    # logging to console and file on daily interval
    logging.basicConfig(format='{asctime}:{levelname}:{name}:{message}', style='{',
                        datefmt='%d-%m-%Y %H:%M:%S', level=logging.INFO,
                        handlers=[logging.StreamHandler(),
                                  TimedRotatingFileHandler(constants.LOG_FILE_PATH, when='D',
                                                           backupCount=3, utc=True)])

    # matplotlib and seaborn
    plt.rcParams['figure.figsize'] = 7.0, 3.5
    sns.set()
    options = {
        'axes.edgecolor': '#A0A0C5',
        'axes.spines.top': False,
        'axes.spines.right': False,
    }
    sns.set_style('darkgrid', options)

    # Download fonts if necessary
    font_downloader.maybe_download()


async def sync_app_commands(bot):
    """Register the slash command tree with Discord.

    A global sync can take up to an hour to propagate, which makes it useless
    while developing. Setting SLASH_COMMAND_GUILD_ID copies the commands into
    that single guild instead, where they show up immediately.
    """
    guild_id = environ.get('SLASH_COMMAND_GUILD_ID')
    if guild_id:
        guild = discord.Object(id=int(guild_id))
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        logging.info(f'Synced {len(synced)} app commands to guild {guild_id}')

        # Discord merges globally registered commands into every guild's
        # command picker, so anything a previous deployment registered globally
        # is still offered here alongside the guild set. Invoking one that this
        # build no longer defines fails with CommandNotFound. Syncing to a
        # guild does not touch the global scope, so clear it explicitly.
        # Checked first because a global sync is a write worth skipping on the
        # usual restart, where there is nothing to remove.
        stale = await bot.tree.fetch_commands()
        if stale:
            bot.tree.clear_commands(guild=None)
            await bot.tree.sync()
            logging.info(f'Removed {len(stale)} stale global app commands: '
                         f'{", ".join(sorted(command.name for command in stale))}')
    else:
        synced = await bot.tree.sync()
        logging.info(f'Synced {len(synced)} app commands globally, '
                     'which can take up to an hour to appear')


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--nodb', action='store_true')
    args = parser.parse_args()

    token = environ.get('BOT_TOKEN')
    if not token:
        logging.error('Token required')
        return

    setup()
    
    # The privileged Message Content intent is deliberately not requested, so
    # that TLE can run on deployments where it is not granted. Discord then only
    # delivers message content for messages that mention the bot, which means
    # the ';' prefix below effectively only fires on '@TLE <command>'. Commands
    # are being migrated to slash commands, which need no message content.
    # See NoMessageContentIntent.md.
    #
    # The Members intent is still privileged and still requested; removing it is
    # a separate phase.
    intents = discord.Intents.default()
    intents.members = True

    bot = commands.Bot(command_prefix=commands.when_mentioned_or(discord_common._BOT_PREFIX), intents=intents)
    bot.help_command = discord_common.TleHelp()
    cogs = [file.stem for file in Path('tle', 'cogs').glob('*.py')]
    for extension in cogs:
        await bot.load_extension(f'tle.cogs.{extension}')
    logging.info(f'Cogs loaded: {", ".join(bot.cogs)}')

    def no_dm_check(ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage('Private messages not permitted.')
        return True

    # Restrict bot usage to inside guild channels only.
    bot.add_check(no_dm_check)

    # A slash command must be acknowledged within 3 seconds or Discord tells the
    # user the bot did not respond. Plenty of TLE commands take longer than that
    # (Codeforces API calls, matplotlib rendering), so defer every interaction
    # up front instead of sprinkling defers across ~118 commands.
    # Context.defer() does nothing for prefix invocations, so this is safe on
    # both paths. Two consequences worth remembering: after a defer ctx.send()
    # posts a followup rather than the initial response, and a command that
    # sends nothing at all will leave Discord showing 'thinking...' forever.
    @bot.before_invoke
    async def defer_interaction(ctx):
        if ctx.interaction is not None:
            await ctx.defer()

    # cf_common.initialize needs to run first, so it must be set as the bot's
    # on_ready event handler rather than an on_ready listener.
    @discord_common.on_ready_event_once(bot)
    async def init():
        await cf_common.initialize(args.nodb)
        await sync_app_commands(bot)
        asyncio.create_task(discord_common.presence(bot))

    bot.add_listener(discord_common.bot_error_handler, name='on_command_error')
    await bot.start(token)


if __name__ == '__main__':
     asyncio.run(main())
