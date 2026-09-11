import functools
import time
import traceback

import discord
from discord.ext import commands

from tle import constants
from tle.util import codeforces_common as cf_common


async def _send_possibly_late(ctx, content):
    """Send a message that may arrive after the interaction token has expired.

    A cache reload can run for many minutes -- the README puts
    `cache problemsets all` at around ten -- while an interaction token is only
    good for 15. Once it lapses, ctx.send() is a followup against a dead token
    and fails, so fall back to a plain message in the channel.
    """
    try:
        await ctx.send(content)
    except discord.HTTPException:
        await ctx.channel.send(content)


def timed_command(coro):
    @functools.wraps(coro)
    async def wrapper(cog, ctx, *args):
        await ctx.send('Running...')
        begin = time.time()
        await coro(cog, ctx, *args)
        elapsed = time.time() - begin
        await _send_possibly_late(ctx, f'Completed in {elapsed:.2f} seconds')

    return wrapper


class CacheControl(commands.Cog):
    """Cog to manually trigger update of cached data. Intended for dev/admin use."""

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_group(brief='Commands to force reload of cache',
                           invoke_without_command=True)
    @commands.has_role(constants.TLE_ADMIN)
    async def cache(self, ctx):
        await ctx.send_help('cache')

    @cache.command(brief='Reload the cached contest list')
    @commands.has_role(constants.TLE_ADMIN)
    @timed_command
    async def contests(self, ctx):
        await cf_common.cache2.contest_cache.reload_now()

    @cache.command(brief='Reload the cached problem list')
    @commands.has_role(constants.TLE_ADMIN)
    @timed_command
    async def problems(self, ctx):
        # DB: problem
        await cf_common.cache2.problem_cache.reload_now()

    @cache.command(brief='Reload cached rating changes', usage='[missing|all|contest_id]')
    @commands.has_role(constants.TLE_ADMIN)
    @timed_command
    async def ratingchanges(self, ctx, contest_id='missing'):
        """Defaults to 'missing'. Mode 'all' clears existing cached changes.
        Mode 'contest_id' clears existing changes with the given contest id.
        """
        if contest_id not in ('all', 'missing'):
            try:
                contest_id = int(contest_id)
            except ValueError:
                return
        if contest_id == 'all':
            await ctx.send('This will take a while')
            count = await cf_common.cache2.rating_changes_cache.fetch_all_contests()
        elif contest_id == 'missing':
            await ctx.send('This may take a while')
            count = await cf_common.cache2.rating_changes_cache.fetch_missing_contests()
        else:
            count = await cf_common.cache2.rating_changes_cache.fetch_contest(contest_id)
        await _send_possibly_late(
            ctx, f'Done, fetched {count} changes and recached handle ratings')

    @cache.command(brief='Reload cached problemsets', usage='contest_id|all')
    @commands.has_role(constants.TLE_ADMIN)
    @timed_command
    async def problemsets(self, ctx, contest_id):
        """Mode 'all' clears all existing cached problems. Mode 'contest_id'
        clears existing problems with the given contest id.
        """
        # DB: problem2
        if contest_id == 'all':
            await ctx.send('This will take a while')
            count = await cf_common.cache2.problemset_cache.update_for_all()
        else:
            try:
                contest_id = int(contest_id)
            except ValueError:
                return
            count = await cf_common.cache2.problemset_cache.update_for_contest(contest_id)
        await _send_possibly_late(ctx, f'Done, fetched {count} problems')


async def setup(bot):
    await bot.add_cog(CacheControl(bot))
