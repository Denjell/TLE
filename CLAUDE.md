# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

TLE is a Discord bot for competitive programming, built on discord.py, centered
around the Codeforces API. Functionality is split into cogs
(`tle/cogs/*.py`), each loaded as a discord.py extension.

## Commands

Dependency management is via Poetry; Python 3.11+ is required.

```bash
poetry install                 # install dependencies
./run.sh                       # install + run the bot (loops: restarts on exit code 42)
./run-pip.sh                   # pip-based alternative when poetry install fails on a system
python -m tle                  # run directly (after `poetry install`/`poetry shell`)
python -m tle --nodb           # run with a DummyUserDbConn instead of the real sqlite user DB
```

Configuration lives in an `environment` file (`cp environment.template environment`,
then fill in `BOT_TOKEN`, `LOGGING_COG_CHANNEL_ID`, `TLE_ADMIN`, `TLE_MODERATOR`,
`SLASH_COMMAND_GUILD_ID`, `VENV_DIR`). Setting `SLASH_COMMAND_GUILD_ID` syncs
slash commands to a single guild for instant iteration instead of the ~1 hour
global propagation delay.

Linting is via `ruff` (no repo-level config file, so it runs with defaults;
`tle/util/codeforces_api.py` disables `N815` inline since its fields
intentionally mirror the Codeforces API's camelCase JSON keys).

After changing a cog's command signatures/descriptions, validate them offline
against Discord's application-command limits before syncing to the real API:

```bash
python extra/check_app_commands.py          # checks every cog (needs full deps)
python extra/check_app_commands.py meta     # check just one cog, during a migration
```

`pytest` is a declared dev dependency, but note this branch currently has no
test sources checked into git under `tests/` (only stale `__pycache__`
directories remain locally) — verify with `git ls-files tests/` before
assuming a test suite exists to run.

## Architecture

### Command surface: hybrid commands, no Message Content intent

Cogs define commands with `@commands.hybrid_command`/`hybrid_group`, so each
one works both as a `/slash` command and as a classic `;prefix` command. The
bot does **not** request the privileged `Message Content` intent (see
`NoMessageContentIntent.md` for the full migration inventory), so Discord only
delivers text content for messages that `@mention` the bot — meaning
`;command` only works as `@TLE command`; everyone else should use `/command`.
`tle/util/discord_common.py`'s `TleHelp` formatter and `describe_invocation`
reflect this (signatures are rendered in slash form; logs reconstruct the
invocation from parsed args since raw message content isn't available).

The bot requests **no privileged intents** at all: the `Server Members`
intent is off too (see `NoServerMembersIntent.md`). The member cache is
therefore near-empty and member join/leave events never fire — don't use
`guild.get_member()` as a membership test or `guild.members` as a member list.
Use `discord_common.fetch_members`/`fetch_member` (batched gateway lookup by
id), `fetch_member_or_user` for records that outlive membership (duels), or
`discord_common.mention(id)` when only a mention is needed. Guild membership is
the DB's `user_handle.active` flag, kept current by the hourly
`Handles._reconcile_status` task (also `/updatestatus`).

`tle/__main__.py` loads cogs by globbing `tle/cogs/*.py` **non-recursively**,
so `tle/cogs/deactivated/*.py` (e.g. `cses.py`) is present in the tree but
never loaded — that's how a cog gets "turned off" without deleting it.
`sync_app_commands()` there also clears any stale globally-registered
commands from previous deployments when syncing to a single dev guild, since
Discord otherwise merges global and guild command sets.

Because Discord requires an interaction to be acknowledged within 3 seconds,
`__main__.py` registers a `before_invoke` hook that defers every interaction
up front rather than sprinkling `ctx.defer()` across every command.

Permission-gated commands use `@commands.has_role`/`has_any_role` against
`constants.TLE_ADMIN`/`TLE_MODERATOR` (role names configurable via env vars,
default `"Admin"`/`"Moderator"`).

### Global state and initialization

`tle/util/codeforces_common.py` holds process-wide singletons (`user_db`,
`cache2`, `event_sys`) set up once by `cf_common.initialize()`, which runs
from the bot's `on_ready` handler (must run before anything else, so it's
wired as the `on_ready` handler itself rather than a listener). It also holds
cross-cutting helpers used throughout the cogs: `resolve_handles` (turns
`!discord-mention` / raw CF handle / `+server` into resolved CF handles),
`SubFilter` (the submission-filter data object — rating bounds, tags, date
range, submission types, etc.), and contest classification helpers
(`is_nonstandard_contest`, `is_rated_for_onsite_contest`).

`SubFilter` used to be populated by parsing a prefix-command string DSL
(`+tag`, `~tag`, `r>=1500`, `d>=012024`, …) via `SubFilter.parse()` and the
standalone `parse_tags`/`parse_rating`/`parse_daterange`/`filter_flags`
helpers in the same file. That DSL made sense as words in a chat message; it
does not exist as a slash-command concept, so every command that used to read
it now takes labelled options instead (`tags`, `min_rating`, `after`, …) and
builds the `SubFilter` directly via `tle/util/filters.py`'s
`build_sub_filter()`. The old string-parsing functions are still defined in
`codeforces_common.py` but are dead code — no cog calls them any more; treat
them as removal candidates rather than a pattern to extend.

### The filter/option layer (`tle/util/filters.py`)

Any command that takes Codeforces tags, a division, a rating range, a date
range, submission types, or a `SubFilter` should build its options from here
rather than re-declaring them: `Division`/`ProblemRating`/`SubmissionRating`
types, `describe()` for the standard option descriptions, `tag_autocomplete`/
`submission_type_autocomplete`, `split_tags`/`problem_tags`/`split_types`,
`parse_date`/`date_range`, and `build_sub_filter()`. Division tags (`div1`…
`edu`) are deliberately kept out of the free-text `tags` option — TLE writes
them into `Problem.tags` itself when caching problemsets, so a plain tag
filter would silently accept them, and for `/gitgud` specifically a tag costs
200 points while the dedicated `division` option costs nothing.

### Caching (`tle/util/cache_system2.py`, `tle/util/db/cache_db_conn.py`)

`CacheSystem` composes several sub-caches (contests, problems, problemsets,
rating changes, ranklists), each backed by a sqlite table via
`CacheDbConn` and refreshed by background tasks defined with
`tle/util/tasks.py`'s `task_spec`/`Waiter` abstraction (fixed-delay reload,
faster retry after an exception, event-triggered reload, etc). Cache updates
dispatch events (`ContestListRefresh`, `RatingChangesUpdate`) through
`tle/util/events.py`'s small pub/sub `EventSystem`, which other parts of the
bot (e.g. duel/training flows) `wait_for` or listen on instead of polling.

### Persistence (`tle/util/db/user_db_conn.py`)

A single sqlite file (`data/db/user.db`) stores per-guild user data: CF handle
bindings, duels, training sessions, gitgud/rated-VC state, etc. (see the
`IntEnum`s at the top of the file for the state machines each feature uses).
`DummyUserDbConn` is a no-op stand-in used with `--nodb`, and
`db.DatabaseDisabledError` is how commands signal "this needs the DB" so
`discord_common.bot_error_handler` can show a consistent message.

### Codeforces API layer (`tle/util/codeforces_api.py`, `tle/util/ranklist/`)

`codeforces_api.py` wraps the CF REST API with `NamedTuple` response models
whose fields intentionally match the API's camelCase JSON (hence the ruff
`N815` suppression), plus the `RATED_RANKS` table used everywhere rank colors
or thresholds are needed. `tle/util/ranklist/rating_calculator.py` implements
Codeforces' own rating-delta algorithm (used for unofficial/rated virtual
contests where CF doesn't compute deltas itself).

### Data/log locations

Paths are centralized in `tle/constants.py`: `data/db`, `data/assets/fonts`
(CJK fonts, auto-downloaded by `tle/util/font_downloader.py` since they're
too large to commit), `data/misc` (e.g. `contest_writers.json`, generated
offline by `extra/scrape_cf_contest_writers.py` to stop the bot recommending
a problem to its own author), `data/temp`, and `logs/`. All of `data/` and
`logs/` are gitignored.
