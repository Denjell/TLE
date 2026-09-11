import asyncio
import logging
import os

from discord.ext import commands

from tle.util import discord_common

root_logger = logging.getLogger()
logger = logging.getLogger(__name__)


class Logging(commands.Cog, logging.Handler):
    def __init__(self, bot, channel_id):
        logging.Handler.__init__(self)
        self.bot = bot
        self.channel_id = channel_id
        self.queue = asyncio.Queue()
        self.task = None
        self.logger = logging.getLogger(self.__class__.__name__)

    @commands.Cog.listener()
    @discord_common.once
    async def on_ready(self):
        self.task = asyncio.create_task(self._log_task())
        width = 79
        stars, msg = f'{"*" * width}', f'***{"Bot running":^{width - 6}}***'
        self.logger.log(level=100, msg=stars)
        self.logger.log(level=100, msg=msg)
        self.logger.log(level=100, msg=stars)

    async def _log_task(self):
        while True:
            record = await self.queue.get()
            channel = self.bot.get_channel(self.channel_id)
            if channel is None:
                # Channel no longer exists.
                root_logger.removeHandler(self)
                self.logger.warning('Logging channel not available, disabling Discord log handler.')
                break
            try:
                msg = self.format(record)
                # Not every record comes from a command invocation, and slash
                # command invocations have no message to jump to, so report
                # whichever of the two is present.
                context_lines = []
                invocation = getattr(record, 'invocation', None)
                if invocation:
                    context_lines.append(f'Original Command: {invocation}')
                jump_url = getattr(record, 'jump_url', None)
                if jump_url:
                    context_lines.append(f'Jump Url: {jump_url}')
                if context_lines:
                    await channel.send('\n'.join(context_lines))
                discord_msg_char_limit = 2000
                char_limit = discord_msg_char_limit - 2 * len('```')
                too_long = len(msg) > char_limit
                msg = msg[:char_limit]
                await channel.send('```{}```'.format(msg))
                if too_long:
                    await channel.send('`Check logs for full stack trace`')
            except:
                self.handleError(record)

    # logging.Handler overrides below.

    def emit(self, record):
        self.queue.put_nowait(record)

    def close(self):
        if self.task:
            self.task.cancel()


async def setup(bot):
    logging_cog_channel_id = os.environ.get('LOGGING_COG_CHANNEL_ID')
    if logging_cog_channel_id is None:
        logger.info('Skipping installation of logging cog as logging channel is not provided.')
        return

    logging_cog = Logging(bot, int(logging_cog_channel_id))
    logging_cog.setLevel(logging.WARNING)
    logging_cog.setFormatter(logging.Formatter(fmt='{asctime}:{levelname}:{name}:{message}',
                                               style='{', datefmt='%d-%m-%Y %H:%M:%S'))
    root_logger.addHandler(logging_cog)
    await bot.add_cog(logging_cog)
