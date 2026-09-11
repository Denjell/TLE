"""Offline check of TLE's slash command tree.

Loads cogs and walks the application command tree without connecting to
Discord, then verifies the limits Discord enforces server-side. Run it after
converting a cog to hybrid commands:

    python extra/check_app_commands.py            # every cog
    python extra/check_app_commands.py meta       # just one, during a migration

Checking every cog needs the full dependency set installed (matplotlib,
seaborn, lxml, pillow, pycairo, PyGObject), so in a partial dev environment
name the cogs you touched. Cogs that fail to import are reported and counted
as a failure rather than silently skipped.

Why this exists: discord.py validates command *names* when the command object
is constructed, so a bad name raises on import. It does not validate
*descriptions* at all -- an over-long one is happily accepted locally and then
rejected by the API with a 400 during tree.sync(), which is a slow and
annoying way to find out. Everything checked below is a limit that would
otherwise only surface against the live API.
"""

import asyncio
import os
import sys
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

# Discord's documented application command limits.
MAX_TOP_LEVEL_COMMANDS = 100
MAX_SUBCOMMANDS_PER_GROUP = 25
MAX_OPTIONS_PER_COMMAND = 25
MAX_NAME_LENGTH = 32
MAX_DESCRIPTION_LENGTH = 100
MAX_NESTING_DEPTH = 2  # group -> subgroup -> command


def check_description(what, description, problems):
    if not description:
        problems.append(f'{what}: description is empty')
    elif len(description) > MAX_DESCRIPTION_LENGTH:
        problems.append(
            f'{what}: description is {len(description)} chars, limit is '
            f'{MAX_DESCRIPTION_LENGTH}\n      {description!r}')


def check_command(command, problems, depth=0, path=''):
    qualified = f'{path}{command.name}'
    if len(command.name) > MAX_NAME_LENGTH:
        problems.append(f'/{qualified}: name is {len(command.name)} chars, '
                        f'limit is {MAX_NAME_LENGTH}')

    if isinstance(command, app_commands.Group):
        check_description(f'/{qualified}', command.description, problems)
        children = command.commands
        if len(children) > MAX_SUBCOMMANDS_PER_GROUP:
            problems.append(f'/{qualified}: {len(children)} subcommands, limit '
                            f'is {MAX_SUBCOMMANDS_PER_GROUP}')
        if depth + 1 > MAX_NESTING_DEPTH:
            problems.append(f'/{qualified}: nested {depth + 1} levels deep, '
                            f'limit is {MAX_NESTING_DEPTH}')
        seen = set()
        for child in children:
            if child.name in seen:
                problems.append(f'/{qualified}: duplicate subcommand '
                                f'{child.name!r}')
            seen.add(child.name)
            check_command(child, problems, depth + 1, f'{qualified} ')
        return 1 + sum(count_commands(child) for child in children)

    check_description(f'/{qualified}', command.description, problems)
    params = command.parameters
    if len(params) > MAX_OPTIONS_PER_COMMAND:
        problems.append(f'/{qualified}: {len(params)} options, limit is '
                        f'{MAX_OPTIONS_PER_COMMAND}')
    for param in params:
        check_description(f'/{qualified} option {param.name!r}',
                          param.description, problems)
    return 1


def count_commands(command):
    if isinstance(command, app_commands.Group):
        return 1 + sum(count_commands(child) for child in command.commands)
    return 1


async def build_tree(cog_names):
    intents = discord.Intents.default()
    intents.members = True
    bot = commands.Bot(command_prefix=';', intents=intents)
    failures = []
    for extension in cog_names:
        try:
            await bot.load_extension(f'tle.cogs.{extension}')
        except Exception as error:  # noqa: BLE001 - report, don't abort
            cause = error.__cause__ or error
            failures.append((extension, f'{type(cause).__name__}: {cause}'))
    return bot, failures


async def run(cog_names):
    all_cogs = sorted(file.stem for file in Path('tle', 'cogs').glob('*.py'))
    if cog_names:
        unknown = sorted(set(cog_names) - set(all_cogs))
        if unknown:
            print(f'No such cog(s): {", ".join(unknown)}')
            print(f'Available: {", ".join(all_cogs)}')
            return 2
    else:
        cog_names = all_cogs

    bot, failures = await build_tree(cog_names)

    top_level = bot.tree.get_commands()
    problems = []
    seen = set()
    for command in top_level:
        if command.name in seen:
            problems.append(f'/{command.name}: duplicate top-level command')
        seen.add(command.name)
        check_command(command, problems)

    total = sum(count_commands(command) for command in top_level)
    if len(top_level) > MAX_TOP_LEVEL_COMMANDS:
        problems.append(f'{len(top_level)} top-level commands, limit is '
                        f'{MAX_TOP_LEVEL_COMMANDS}')

    prefix_only = [command.qualified_name for command in bot.walk_commands()
                   if not isinstance(command, commands.HybridCommand | commands.HybridGroup)]

    print(f'Cogs checked:           {len(bot.cogs)} of {len(cog_names)}')
    print(f'Prefix commands:        {len(list(bot.walk_commands()))}')
    print(f'App commands total:     {total}')
    print(f'App commands top-level: {len(top_level)} / {MAX_TOP_LEVEL_COMMANDS}')
    print()
    print(f'Still prefix-only ({len(prefix_only)}):')
    for name in sorted(prefix_only):
        print(f'  ;{name}')

    if failures:
        print()
        print(f'{len(failures)} cog(s) failed to load, and were NOT checked:')
        for extension, reason in failures:
            print(f'  - {extension}: {reason}')

    if problems:
        print()
        print(f'{len(problems)} problem(s) Discord would reject:')
        for problem in problems:
            print(f'  - {problem}')

    print()
    if failures or problems:
        print('FAILED')
        return 1
    print('No problems found.')
    return 0


if __name__ == '__main__':
    # Run from the repo root so the 'tle.cogs.*' glob and imports resolve
    # regardless of where the script was invoked from.
    repo_root = Path(__file__).resolve().parent.parent
    os.chdir(repo_root)
    sys.path.insert(0, str(repo_root))
    sys.exit(asyncio.run(run(sys.argv[1:])))
