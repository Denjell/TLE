import asyncio
import functools
import logging
import random
from collections.abc import Callable, Iterable
from typing import Any

import discord
from discord.ext import commands

from tle import constants
from tle.util import codeforces_api as cf, db, tasks

logger = logging.getLogger(__name__)

_CF_COLORS = (0xFFCA1F, 0x198BCC, 0xFF2020)
_SUCCESS_GREEN = 0x28A745
_ALERT_AMBER = 0xFFBF00
_BOT_PREFIX = ';'


def embed_neutral(desc: object, color: int | None = None) -> discord.Embed:
    return discord.Embed(description=str(desc), color=color)


def embed_success(desc: object) -> discord.Embed:
    return discord.Embed(description=str(desc), color=_SUCCESS_GREEN)


def embed_alert(desc: object) -> discord.Embed:
    return discord.Embed(description=str(desc), color=_ALERT_AMBER)


def random_cf_color() -> int:
    return random.choice(_CF_COLORS)


def cf_color_embed(**kwargs: Any) -> discord.Embed:
    return discord.Embed(**kwargs, color=random_cf_color())


def set_same_cf_color(embeds: list[discord.Embed]) -> None:
    color = random_cf_color()
    for embed in embeds:
        embed.color = color


def attach_image(embed: discord.Embed, img_file: discord.File) -> None:
    embed.set_image(url=f'attachment://{img_file.filename}')


def set_author_footer(
    embed: discord.Embed, user: discord.Member | discord.User
) -> None:
    embed.set_footer(text=f'Requested by {user}', icon_url=user.display_avatar.url)


def get_role(guild: discord.Guild, role_identifier: str | int) -> discord.Role | None:
    """Look up a role by name (str) or ID (int)."""
    if isinstance(role_identifier, int):
        return guild.get_role(role_identifier)
    return discord.utils.get(guild.roles, name=role_identifier)


def has_role(member: discord.Member, role_identifier: str | int) -> bool:
    """Check if member has a role identified by name (str) or ID (int)."""
    if isinstance(role_identifier, int):
        return any(role.id == role_identifier for role in member.roles)
    return any(role.name == role_identifier for role in member.roles)


async def fetch_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    """Like `Guild.get_member`, but falls back to a REST fetch when the
    member isn't in the local cache.

    We don't request the privileged Members intent, so the gateway-populated
    member cache is incomplete (only members who have recently interacted
    are in it); this fills the gap with a `GET /guilds/{id}/members/{id}`
    call, which needs no privileged intent. Returns None if the member has
    left the guild or the ID is invalid.
    """
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        return None
    except discord.HTTPException:
        logger.warning(f'fetch_member failed for user {user_id} in guild {guild.id}')
        return None


async def fetch_members_by_ids(
    guild: discord.Guild, user_ids: Iterable[int]
) -> dict[int, discord.Member]:
    """Resolves the given user ids to guild members, as `{id: member}`.

    Ids belonging to users who have left the guild are simply absent from
    the result.

    Without the privileged Members intent we can neither rely on the
    gateway-populated member cache nor list the guild's members
    (`Guild.fetch_members` / `Guild.chunk` both require that intent).
    Requesting *specific* ids is allowed though, so this asks the gateway
    for them in batches of 100 via `Guild.query_members`, and falls back to
    one REST call per id if that request fails.
    """
    ids = list(dict.fromkeys(user_ids))  # de-duplicate, keep order
    if not ids:
        return {}

    resolved: dict[int, discord.Member] = {}
    pending: list[int] = []
    for user_id in ids:
        member = guild.get_member(user_id)
        if member is not None:
            resolved[user_id] = member
        else:
            pending.append(user_id)

    for i in range(0, len(pending), 100):
        chunk = pending[i : i + 100]
        try:
            for member in await guild.query_members(user_ids=chunk, limit=100):
                resolved[member.id] = member
        except (discord.ClientException, discord.HTTPException, asyncio.TimeoutError):
            logger.warning(
                f'query_members failed for {len(chunk)} ids in guild {guild.id},'
                ' falling back to individual fetches.',
                exc_info=True,
            )
            for user_id in chunk:
                member = await fetch_member(guild, user_id)
                if member is not None:
                    resolved[user_id] = member
    return resolved


def send_error_if(*error_cls: type[Exception]) -> Callable[..., Any]:
    """Decorator for `cog_command_error` methods.

    Decorated methods send the error in an alert embed when the error is an
    instance of one of the specified errors, otherwise the wrapped function is
    invoked.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(cog: Any, ctx: commands.Context, error: Exception) -> None:
            if isinstance(error, error_cls):
                await ctx.send(embed=embed_alert(error))
                error.handled = True
            else:
                await func(cog, ctx, error)

        return wrapper

    return decorator


async def bot_error_handler(ctx: commands.Context, exception: Exception) -> None:
    if getattr(exception, 'handled', False):
        # Errors already handled in cogs should have .handled = True
        return

    if isinstance(exception, db.DatabaseDisabledError):
        await ctx.send(
            embed=embed_alert(
                'Sorry, the database is not available. Some features are disabled.'
            )
        )
    elif isinstance(exception, commands.NoPrivateMessage):
        await ctx.send(embed=embed_alert('Commands are disabled in private channels'))
    elif isinstance(exception, commands.DisabledCommand):
        await ctx.send(embed=embed_alert('Sorry, this command is temporarily disabled'))
    elif isinstance(exception, (cf.CodeforcesApiError, commands.UserInputError)):
        await ctx.send(embed=embed_alert(exception))
    else:
        msg = 'Ignoring exception in command {}:'.format(ctx.command)
        exc_info = type(exception), exception, exception.__traceback__
        extra = {
            'message_content': ctx.message.content,
            'jump_url': ctx.message.jump_url,
        }
        logger.exception(msg, exc_info=exc_info, extra=extra)


def once(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that wraps a coroutine such that it is executed only once."""
    first = True

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> None:
        nonlocal first
        if first:
            first = False
            await func(*args, **kwargs)

    return wrapper


async def presence(bot: Any) -> None:
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening, name='your commands'
        )
    )
    await asyncio.sleep(60)

    @tasks.task(name='OrzUpdate', waiter=tasks.Waiter.fixed_delay(10 * 60))
    async def presence_task(_: Any) -> None:
        guilds = list(bot.guilds)
        if not guilds:
            return
        guild = random.choice(guilds)
        # Without the Members intent we can't enumerate the guild, so pick
        # from the users who registered a handle instead.
        user_ids = [
            user_id for user_id, _ in await bot.user_db.get_handles_for_guild(guild.id)
        ]
        if not user_ids:
            return
        random.shuffle(user_ids)
        for user_id in user_ids[:10]:
            target = await fetch_member(guild, user_id)
            if target is not None and not has_role(target, constants.TLE_PURGATORY):
                await bot.change_presence(
                    activity=discord.Game(name=f'{target.display_name} orz')
                )
                return

    presence_task.start()


class TleHelp(commands.DefaultHelpCommand):
    def add_command_formatting(self, command: commands.Command[Any, ..., Any]) -> None:
        """Format the non-indented block of commands and groups, showing the
        bot's command prefix in the usage signature.
        """
        if command.description:
            self.paginator.add_line(command.description, empty=True)

        signature = _BOT_PREFIX + command.qualified_name
        if len(command.aliases) > 0:
            aliases = '|'.join(command.aliases)
            signature += '|' + aliases
        if command.usage:
            signature += ' ' + command.usage
        self.paginator.add_line(signature, empty=True)

        if command.help:
            try:
                self.paginator.add_line(command.help, empty=True)
            except RuntimeError:
                for line in command.help.splitlines():
                    self.paginator.add_line(line)
                self.paginator.add_line()
