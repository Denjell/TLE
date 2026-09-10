"""Integration tests validating every app command against Discord's limits.

Discord rejects the whole command tree with a single HTTP 400 (error code
50035) if any one command violates a limit, so a single bad ``brief`` takes
down startup for all of them. These checks catch that before the sync.
"""

import os
import pkgutil
import re
import sys

import discord
import pytest
from discord import app_commands
from discord.ext import commands

import tle.cogs

pytestmark = pytest.mark.integration

# https://discord.com/developers/docs/interactions/application-commands
_NAME_RE = re.compile(r'^[-_\w]{1,32}$')
_MAX_DESCRIPTION = 100
_MAX_OPTIONS = 25
_MAX_DEPTH = 2


@pytest.fixture(scope='module')
async def command_tree():
    os.environ.setdefault('BOT_TOKEN', 'x')
    os.environ.setdefault('LOGGING_COG_CHANNEL_ID', '1')

    # load_extension re-executes the cog module and rebinds it in sys.modules,
    # even when it is already imported. Other test modules hold references to
    # the classes from the original module objects, so a mock.patch of
    # 'tle.cogs.X.dependency' would land on our replacement and silently miss.
    # Snapshot and restore so this module stays side effect free.
    saved = {name: mod for name, mod in sys.modules.items() if name.startswith('tle.')}

    bot = commands.Bot(command_prefix=';', intents=discord.Intents.default())
    for module in pkgutil.iter_modules(tle.cogs.__path__):
        await bot.load_extension(f'tle.cogs.{module.name}')
    try:
        yield bot.tree.get_commands()
    finally:
        for name in [n for n in sys.modules if n.startswith('tle.')]:
            if name in saved:
                sys.modules[name] = saved[name]
            else:
                del sys.modules[name]


def _walk(command, prefix='', depth=1):
    """Yields (qualified_name, depth, command) for the command and its
    subcommands.
    """
    name = f'{prefix}{command.name}'
    yield name, depth, command
    if isinstance(command, app_commands.Group):
        for sub in command.commands:
            yield from _walk(sub, f'{name} ', depth + 1)


def _all_commands(tree):
    for command in tree:
        yield from _walk(command)


async def test_cogs_produce_app_commands(command_tree):
    assert command_tree, 'No app commands registered'


async def test_names_are_valid(command_tree):
    invalid = [
        name
        for name, _, command in _all_commands(command_tree)
        if not _NAME_RE.match(command.name) or command.name != command.name.lower()
    ]
    assert not invalid, f'Invalid app command names: {invalid}'


async def test_descriptions_within_limits(command_tree):
    bad = [
        (name, len(command.description or ''))
        for name, _, command in _all_commands(command_tree)
        if not 1 <= len(command.description or '') <= _MAX_DESCRIPTION
    ]
    assert not bad, (
        'Descriptions must be 1-100 characters; Discord rejects the entire '
        f'command tree otherwise. Offenders (name, length): {bad}'
    )


async def test_parameter_descriptions_within_limits(command_tree):
    bad = [
        (f'{name} <{param.name}>', len(param.description or ''))
        for name, _, command in _all_commands(command_tree)
        if not isinstance(command, app_commands.Group)
        for param in command.parameters
        if not 1 <= len(param.description or '') <= _MAX_DESCRIPTION
    ]
    assert not bad, f'Parameter descriptions must be 1-100 characters: {bad}'


async def test_parameter_names_are_valid(command_tree):
    invalid = [
        f'{name} <{param.name}>'
        for name, _, command in _all_commands(command_tree)
        if not isinstance(command, app_commands.Group)
        for param in command.parameters
        if not _NAME_RE.match(param.name) or param.name != param.name.lower()
    ]
    assert not invalid, f'Invalid parameter names: {invalid}'


async def test_option_count_within_limit(command_tree):
    bad = [
        (name, len(command.parameters))
        for name, _, command in _all_commands(command_tree)
        if not isinstance(command, app_commands.Group)
        and len(command.parameters) > _MAX_OPTIONS
    ]
    assert not bad, f'Commands may have at most {_MAX_OPTIONS} options: {bad}'


async def test_nesting_depth_within_limit(command_tree):
    too_deep = [
        (name, depth)
        for name, depth, _ in _all_commands(command_tree)
        if depth > _MAX_DEPTH
    ]
    assert not too_deep, (
        f'Discord allows at most {_MAX_DEPTH} levels of subcommands: {too_deep}'
    )
