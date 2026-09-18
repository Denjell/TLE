# Migration plan: running TLE without the Message Content intent

Branch: `NoPrivilegedIntents` (branched from `master` @ `ed4d892`)

This document covers **phase 1 only: the `MESSAGE CONTENT` privileged intent**.
The `SERVER MEMBERS` intent is a separate phase and is deliberately out of scope
here; see [Out of scope](#out-of-scope).

This plan is written against the code on `master`.

---

## 1. Goal

Remove `intents.message_content = True` from [tle/\_\_main\_\_.py:61](tle/__main__.py#L61)
and keep the bot fully functional, so TLE can run on deployments where the
privileged intent is not or cannot be granted (unverified bot, >100 guilds
without approval, or a server owner who refuses it).

## 2. What the intent actually gates

Without `MESSAGE_CONTENT`, Discord returns **empty** `content`, `attachments`,
`embeds` and `components` on message objects. This applies to **both** gateway
events and REST fetches (`channel.fetch_message`), which matters for the
starboard.

Three exceptions still deliver content:

1. Messages **authored by the bot itself**.
2. Messages in a **DM with the bot**.
3. Messages that **@mention the bot**.

Exception 3 is the important one: `command_prefix=commands.when_mentioned_or(';')`
([\_\_main\_\_.py:63](tle/__main__.py#L63)) means `@TLE gimme 1500` keeps working
even after the intent is dropped, while `;gimme 1500` silently does nothing.
That gives us a working escape hatch during the migration, but it is not an
acceptable end state for users.

discord.py also emits a startup warning when a non-mention prefix is configured
without the intent (`Privileged message content intent is missing, commands may
not work as expected.`) — expected noise, not a failure.

## 3. Inventory — everything on `master` that depends on message content

### 3.1 The command surface (the bulk of the work)

The bot is **100% prefix commands**. There is not a single `app_commands`,
`hybrid_command` or `bot.tree` usage in `tle/`.

| Metric | Count |
|---|---|
| Command callbacks total | 118 |
| Top-level entries (11 groups + 26 standalone commands) | 37 |
| Largest group (`duel`) | 18 subcommands |

Good news on the Discord limits: 37 top-level entries is well under the 100
global chat-input command cap, 18 is under the 25-subcommand cap, and no group
nests deeper than one level (the 2-level max). **No command has to be dropped or
restructured for capacity reasons.**

Groups: `cache`, `clist`, `remind`, `duel`, `plot`, `handle`, `roleupdate`,
`round`, `meta`, `starboard`, `training`.

### 3.2 Direct `message.content` readers

| Site | What it does | Impact |
|---|---|---|
| [lockout.py:150-165](tle/cogs/lockout.py#L150-L165) `_get_time_response` | `wait_for('message')`, reads `m.content.isdigit()` | **Hard break** — hangs until timeout |
| [lockout.py:168-190](tle/cogs/lockout.py#L168-L190) `_get_seq_response` | `wait_for('message')`, reads `m.content.split()` | **Hard break** |
| [starboard.py:59-60](tle/cogs/starboard.py#L59-L60) `prepare_embed` | Copies starred message text into the embed | **Degraded** — always empty |
| [starboard.py:86](tle/cogs/starboard.py#L86) | Rejects messages with no content **and** no attachments | **Hard break** — rejects ~everything |
| [discord_common.py:89](tle/util/discord_common.py#L89) | Logs `ctx.message.content` on unhandled errors | **Degraded** — logs `''` |
| [logging.py:45-46](tle/cogs/logging.py#L45-L46) | Echoes that into the log channel | **Degraded** |

Note on the error handler: discord.py synthesises a `Message` with `content=''`
for app-command contexts, so nothing crashes — it just stops being useful.

### 3.3 Things that look affected but are not

- **Reaction pagination** ([paginator.py](tle/util/paginator.py)) and
  [lockout.py:129](tle/cogs/lockout.py#L129) `wait_for('reaction_add')` —
  `GUILD_MESSAGE_REACTIONS` is unprivileged. These still work. They do need
  rework for *interaction* reasons (§6.4), which is a different problem.
- **CF-side data** (submissions, compile-error identify flow) — nothing to do
  with Discord message content.
- [tle/cogs/deactivated/cses.py](tle/cogs/deactivated/cses.py) — not loaded, the
  cog glob is `tle/cogs/*.py`.

### 3.4 Documentation

[README.md:80](README.md#L80) instructs the operator to enable both privileged
intents in the Developer Portal. Must be rewritten.

---

## 4. Strategy

### Options considered

| Option | Effort | Result |
|---|---|---|
| **A. Mention-only prefix** — change prefix to `commands.when_mentioned` | ~1 line | Works immediately, but every invocation becomes `@TLE gimme 1500`. Ugly, no autocomplete, no discoverability. |
| **B. Full rewrite to `app_commands`** | Very large | Clean, but throws away the prefix path and every `ctx`-based helper in the codebase. |
| **C. Hybrid commands** — `@commands.hybrid_command` / `hybrid_group` | Large but mechanical | One callback serves both `/gimme` and `@TLE gimme`. Keeps `ctx`, keeps checks, keeps cog structure. |

### Recommendation: **A as an immediate safety net, then C as the real migration.**

Land A first as a one-commit change so the branch is never in a state where the
bot is dead; then convert cog by cog to hybrid. The prefix `;` stays configured
throughout (it just only fires on mention), so nothing regresses for users who
already type `@TLE`.

Option B is rejected: the whole codebase is built around `commands.Context`
(`cf_common.resolve_handles(ctx, ...)`, `discord_common.send_error_if`,
`cog_command_error`, `paginator.paginate(bot, ctx.channel, ...)`), and hybrid
keeps all of it.

---

## 5. Work breakdown

### Stage 0 — Make it not-dead (1 commit)

- Drop `intents.message_content = True` from [\_\_main\_\_.py:61](tle/__main__.py#L61).
- Leave the prefix as `when_mentioned_or(';')`.
- Add a comment recording *why* the intent is absent.
- Update [README.md:80](README.md#L80): no Message Content intent, and the invite
  URL **must include the `applications.commands` scope** (see §6.6).

At this point `@TLE <cmd>` works, `;<cmd>` does not, starboard and
`round challenge` are broken. That is expected and fixed by later stages.

### Stage 1 — Bot plumbing (1 commit)

- `await bot.tree.sync()` on startup. Prefer syncing to a configured guild id
  during development (instant) and a global sync in production (up to ~1h
  propagation).
- Add a **global `before_invoke` hook** that calls `await ctx.defer()` when
  `ctx.interaction is not None`. `Context.defer()` is a documented no-op for
  prefix contexts, so this is safe for both paths and solves the 3-second
  acknowledgement deadline once instead of 118 times (§6.1).
- Fix the error handler ([discord_common.py:85-92](tle/util/discord_common.py#L85-L92)):
  reconstruct the invocation from `ctx.command.qualified_name` + `ctx.kwargs`
  rather than reading `ctx.message.content`, and only emit `jump_url` when
  `ctx.interaction is None`. Adjust [logging.py:43-47](tle/cogs/logging.py#L43-L47) to match.

### Stage 2 — Convert cogs to hybrid (one commit per cog)

**Done:** `meta`, `cache_control`, `contests`, `duel`, `handles`, `graphs`,
`training`, `codeforces`, `lockout` -- 10 of 11, giving 114 application commands
across 36 top-level entries. The help command (6.5) is done too.

**Remaining: `starboard` only**, deliberately deferred. See 6.7.

Suggested order, easiest first, so the mechanical pattern is established before
hitting the hard cogs:

1. `meta` (6 subs, no args) — the pilot.
2. `starboard` (3 subs) + the starboard content fix (§6.7).
3. `cache_control` (4 subs).
4. `contests`, `duel`, `handles`, `graphs`, `training`, `codeforces`.
5. `lockout` last — it needs the interactive-prompt redesign (§6.8).

Per command: `@commands.command` → `@commands.hybrid_command`,
`@commands.group` → `@commands.hybrid_group`. Subcommands of a converted group
inherit hybrid automatically. Groups need a `fallback=` name if the group's own
callback (`ctx.send_help`) should be reachable as a slash command.

### Stage 3 — Paginator and help (1-2 commits)

See §6.4 and §6.5.

### Stage 4 — Docs and verification

See §7.

---

## 6. Problems

These are the parts that are *not* mechanical. Each needs a decision.

### 6.1 The 3-second interaction deadline

A slash command must be acknowledged within 3 seconds or Discord shows *"The
application did not respond"*. Many TLE commands are far slower: `plot *`
(matplotlib render), `stalk`, `fullsolve`, `ranklist`, `gudgitters`,
`vcratings`, anything hitting the CF API cold.

**Fix:** the global `before_invoke` defer in Stage 1.

**Caveats to watch:** after a defer, `ctx.send` becomes a *followup*, which
changes the semantics of `delete_after`. Also, the interaction token expires
after 15 minutes — any command that could exceed that must send a real message
instead of a followup.

**The 15-minute rule needs a pattern, not a one-off.** Any command that replies
*after* long work can outlive its token. `cache_control` solves it with a
`_send_possibly_late()` helper that tries `ctx.send` and falls back to
`ctx.channel.send` on `HTTPException`. Apply the same wherever a reply follows
a long operation — `cache problemsets all` is the documented ~10-minute case,
but `ranklist` on a large contest and the `plot` family can get close.

**Decorators must forward `**kwargs`.** A prefix invocation passes a command's
arguments positionally; an app command invocation passes them by keyword. A
decorator wrapping the callback as `(cog, ctx, *args)` therefore works on the
prefix path and raises `TypeError` on the slash path, which discord.py reports
as `CommandSignatureMismatch` — an error that blames the command tree and never
mentions the decorator. Building the tree does **not** catch this, because
discord.py derives parameters through `__wrapped__`; the registered options look
correct while the call can never succeed. `check_app_commands.py` now inspects
the real callback with `follow_wrapped=False`. Hit in `cache_control`
(`timed_command`); `cf_common.user_guard` was already correct.

### 6.2 Variadic `*args` — 25 commands

Slash commands have no variadic parameter. Affected:

`gimme`, `stalk`, `mashup`, `gitgud`, `vc`, `fullsolve`, `teamrate`,
`ranklist`, `plot rating`, `plot performance`, `plot extreme`, `plot solved`,
`plot hist`, `plot curve`, `plot scatter`, `plot centile`, `plot country`,
`plot visualrank`, `plot speed`, `gudgitters`, `monthlygudgitters`,
`handle list`, `training start`, `training solved`, `training fastest`.

**Fix:** convert `*args` → keyword-only `*, args: str = ''` and `args.split()`
inside. Renders as a single free-text slash option, and prefix behaviour is
almost identical.

**Problem:** it is not *exactly* identical. `*args` goes through discord.py's
argument tokeniser, which strips quotes; a keyword-only `str` receives the raw
remainder. Any command where a user quotes an argument changes behaviour. The
tag/filter syntax these commands use (`+tag`, `~tag`, `d>=012024`, `?-200`) is
unquoted in practice, so the risk is low, but each of the 25 needs its parsing
helper (`cf_common.parse_tags`, the per-cog filter parsers) re-checked to
confirm it receives a list of the same shape.

**Three have since gone further.** `gitgud`, `gimme` and `stalk` now take
labelled options instead of one `args` field: comma separated `tags` /
`exclude_tags` with autocomplete, `division` / `exclude_division` dropdowns,
`Range` rating bounds, and `after` / `before` dates. `stalk` also turns its
four submission-type flags into one autocompleted comma separated field, and
`+hardest` / `+team` / `c+` / `i+` into `sort` / `include_team` / `contests` /
`indices`. The shared helpers live on the `Codeforces` cog
([codeforces.py](tle/cogs/codeforces.py)) and are the pattern to copy for the
rest of the filter commands. This drops the prefix form of those three, which
is acceptable here because nobody mentions the bot to invoke them.

### 6.3 Variadic typed parameters — 7 commands

| Command | Signature | Cap |
|---|---|---|
| [contests.py](tle/cogs/contests.py) `remind here` | `*before: int` | unbounded |
| [contests.py](tle/cogs/contests.py) `ratedvc` | `*members: discord.Member` | unbounded |
| [contests.py](tle/cogs/contests.py) `vcrating` | `*members: discord.Member` | 5 |
| [contests.py](tle/cogs/contests.py) `vcperformance` | `*members: discord.Member` | 5 |
| [duel.py](tle/cogs/duel.py) `duel rating` | `*members: discord.Member` | 5 |
| [graphs.py](tle/cogs/graphs.py) `plot howgud` | `*members: discord.Member` | 5 |
| [lockout.py](tle/cogs/lockout.py) `round challenge` | `*members: discord.Member` | `MAX_ROUND_USERS` |

**Two viable fixes**, and they should not be mixed arbitrarily:

- **Fixed optional parameters** (`member1..member5: discord.Member = None`) for
  the capped ones. Gives proper member pickers in the slash UI. Verbose.
- **A single `str` parameter** parsed with `MemberConverter` for the unbounded
  ones (`ratedvc`, `remind here`). Loses the picker; users paste mentions.

**Decision needed:** whether `ratedvc` and `remind here` get the string
treatment or stay prefix-only (`with_app_command=False`). Prefix-only now means
*mention-only*, so it is a real degradation, not a free pass — recommend the
string parameter.

### 6.4 The paginator — 17 call sites

**Status: the minimum fix is done.** `paginate()` takes an optional `ctx` and
sends the first page through it, which answers the deferred interaction;
callers without one (the rated vc watcher) keep the channel path. Each cog
passes `ctx` as it is converted. The button rework below is still open.


[paginator.py](tle/util/paginator.py) sends the first page with
`channel.send()` from a fire-and-forget `asyncio.create_task`
([paginator.py:87](tle/util/paginator.py#L87)). Under a slash invocation that
posts an unrelated message and never resolves the interaction.

It also hard-requires `manage_messages` ([paginator.py:80-81](tle/util/paginator.py#L80-L81))
purely to strip reactions.

**Fix (minimum):** thread `ctx` into `paginate()` and send the first page via
`ctx.send`, falling back to `channel.send` where no ctx exists
([contests.py:525](tle/cogs/contests.py#L525) passes an explicit `channel`, not
`ctx.channel` — that one is a reminder broadcast and must keep the channel path).

**Fix (better, separate commit):** replace reaction pagination with a
`discord.ui.View` of buttons. Drops the `manage_messages` requirement entirely
and works identically for prefix and slash. This is not *required* by the intent
removal, so it should be its own commit and can be deferred.

### 6.5 `ctx.send_help` and `TleHelp`

All 11 groups call `await ctx.send_help(ctx.command)` in the group callback.
[TleHelp](tle/util/discord_common.py#L142-L160) renders signatures as
`_BOT_PREFIX + command.qualified_name`, i.e. `;duel challenge`.

Once `;` no longer fires, **every help message is telling users to type
something that does not work.** TleHelp must render `/`-prefixed signatures (or
both forms).

Separately: Discord requires a subcommand when invoking a group, so a hybrid
group's own callback is only reachable as a slash command if given a
`fallback=` name. Decide per group whether `/duel help` is worth registering.

### 6.6 Sync, scope, and visibility

- **The invite scope is a deployment gotcha, not a code change.** If TLE was
  invited with only the `bot` scope, slash commands will not appear no matter
  what the code does. The bot must be re-invited with
  `scope=bot%20applications.commands`. This needs to be in the README and
  communicated to server admins before rollout.
- Global sync propagates slowly (up to an hour). Use a guild-scoped sync for
  testing.
- **Admin commands become visible to everyone.** `meta kill`, `meta restart`,
  `roleupdate *`, `starboard here`, `duel _invalidate` etc. are protected by
  `commands.has_role(TLE_ADMIN)`, which still runs — but they now *appear* in
  everyone's slash picker and fail noisily on use. Consider
  `@app_commands.default_permissions(...)` to hide them.

  A check failure also has to *reply*. Checks run before the `before_invoke`
  hook that defers, so a silent `MissingRole` leaves the interaction
  unacknowledged and Discord tells the user the application did not respond —
  while the log channel gets a traceback for what is now routine. Handled in
  `discord_common.bot_error_handler` via a `CheckFailure` branch.

### 6.6a Registrations outlive the code that made them

Found the hard way, and none of it is visible from the source tree:

- **Stale global commands.** A previous deployment synced ~36 commands
  globally. Discord merges global commands into every guild's picker, so they
  sat beside the guild set, and invoking one this build no longer defines
  fails with `CommandNotFound` (the user sees *Unknown integration*). Syncing
  to a guild never touches the global scope. `sync_app_commands` now clears
  global when guild-scoped, but **the production cutover has the same problem
  in reverse**, and there it matters more.
- **The mirror case is unhandled.** Switching from guild-scoped back to global
  leaves the guild registrations in place, and the guild id is not known at
  that point to clear them. Plan the cutover deliberately.
- **Renames orphan their old registration.** `_unregistervc` → `unregistervc`
  left `_unregistervc` registered until something removed it. Every rename in
  §6.9 carries this.
- **Clients cache the command list.** After registrations change, a client may
  still offer a deleted command and fail with *Unknown integration*. A client
  reload (Ctrl+R) fixes it. Worth telling users at rollout, so it is not
  reported as a bot fault.

### 6.7 Starboard — unavoidable degradation

This is the one place where functionality is genuinely lost.

- [starboard.py:86](tle/cogs/starboard.py#L86) rejects a message when
  `len(message.content) == 0 and len(message.attachments) == 0`. Without the
  intent *both* are always empty for other users' messages — including via the
  `channel.fetch_message` REST call at [line 84](tle/cogs/starboard.py#L84).
  **The starboard would silently stop accepting everything.**
- [starboard.py:59-60](tle/cogs/starboard.py#L59-L60) would produce entries with
  no text.
- Reaction *detection* is unaffected (unprivileged intent), so the trigger still
  fires — it is only the content that is gone.

**Status: deliberately left undone (2026-09-13).** Everything else in this
document is finished. Note what that means in practice: the starboard is not
merely unconverted, it is *quietly* broken. The reaction still fires, then
every message is rejected and the failure is logged at info level, so from a
user's side the starboard simply stops working with no explanation. If this
stays unresolved for long, consider at least making the failure visible.

**There is a way to keep full content**, found after this document was first
written: **message context menu commands**. A right-click -> Apps -> "Star this
message" command receives the target message inside the interaction payload
(`interaction.data['resolved']['messages']`, see discord.py's
`app_commands/namespace.py`), rather than through the gateway cache or a REST
fetch. That is Discord's documented route for apps that have dropped the
intent.

It combines well with what still works, because **reaction data is not gated**
-- the intent covers `content`, `attachments`, `embeds` and `components` only,
which is why the existing threshold logic is unaffected.

| Option | Trigger | Content | Cost |
|---|---|---|---|
| **A. Context menu only** | right-click -> Apps -> Star | full | loses the star-threshold model |
| **A+. Reactions + context menu** | people react as now; one person right-clicks to publish | full | one explicit action instead of fully automatic |
| **B. Link-only, automatic** | star threshold, as now | none: channel, author, jump link | fully automatic, entries lose their text |
| Remove the cog | - | - | honest, but loses a feature people use |
| Keep `MESSAGE_CONTENT` on for this alone | - | full | defeats the purpose of this branch |

**A+ is the recommendation.** The bot can still verify "this message has >= 5
stars" when the context menu fires, since it re-fetches the message and
reaction counts survive, so the social mechanic is unchanged and only the final
publish step needs a human action. Message commands have their own quota (5 per
app), so there is no pressure on the 100-command limit.

**Unverified assumption, and the whole design rests on it:** that Discord
populates `content` in that resolved payload for an app *without* the intent.
The plumbing is confirmed in discord.py; the Discord-side behaviour is not.
Test it with a throwaway context menu command before building anything. If
content does not come through, the answer collapses back to B.

### 6.8 Lockout `round challenge` — interactive prompt redesign

[lockout.py:300-308](tle/cogs/lockout.py#L300-L308) runs a five-step chat
wizard: problem count → duration → per-problem ratings → per-problem points →
repeat yes/no, each read from a typed chat message. All five break.

**Options:**

- **Move the parameters into the command signature** with defaults —
  `problems`, `duration`, `ratings` (space-separated string), `points`
  (space-separated string), `repeat`. Simple, scriptable, no interaction state.
  Verbose, and `ratings`/`points` are variable-length so they must be strings
  (§6.2 applies).
- **A `discord.ui.Modal`** — the closest match to the current UX, but a modal can
  only be opened *from an interaction*. For a prefix invocation there is none, so
  the command would have to post a button that opens the modal. Works for both
  paths, but is more moving parts.
- **Button/select wizard** — most work, best UX.

**Recommendation:** signature parameters as the primary path (it also fixes the
30/60-second timeouts that make the current flow fragile), optionally with a
"Configure" button opening a modal later.

Note [lockout.py:115-138](tle/cogs/lockout.py#L115-L138) `_check_if_all_members_ready`
uses reactions on a *bot-authored* message — unaffected by the intent, and can
stay as-is.

### 6.9 Command names that need explicit `name=`

Slash names must be lowercase and match `^[-_\w]{1,32}$`. Leading underscores are
technically legal but look broken in the picker, and several of these exist only
to dodge Python name shadowing:

- [codeforces.py:502](tle/cogs/codeforces.py#L502) `_nogud` — and note `nogud`
  already exists at [line 483](tle/cogs/codeforces.py#L483), so this needs a
  *different* name, e.g. `forceskip`.
- [duel.py:821](tle/cogs/duel.py#L821) `_invalidate` — `invalidate` exists at
  [line 803](tle/cogs/duel.py#L803). Needs e.g. `forceinvalidate`.
- [lockout.py:325](tle/cogs/lockout.py#L325) `_invalidate`.
- [contests.py:667](tle/cogs/contests.py#L667) `_unregistervc`.
- [handles.py:287](tle/cogs/handles.py#L287) `_updatestatus`.

Renaming these changes the prefix command name too. **Better approach than the
renames proposed above:** give the command the clean name and keep the old one
as an `alias`, so the slash command reads properly while existing prefix usage
keeps working. Done for `_unregistervc` → `unregistervc` (alias
`_unregistervc`); apply the same to the rest. `_nogud` and `duel _invalidate`
still need genuinely different names, since `nogud` and `invalidate` are taken.

### 6.10 Descriptions

The slash description limit is 100 characters. discord.py derives a hybrid
command's description from `description or brief or first docstring line`.
Scan results — only two problems, both trivial:

- [duel.py:802](tle/cogs/duel.py#L802) `duel giveup` — `brief` is 147 chars.
  **Registration will fail** until shortened.
- [cache_control.py:35](tle/cogs/cache_control.py#L35) `cache contests` — no
  `brief` and no docstring; discord.py falls back to `'…'`. Legal but should get
  a real description.

Every group also needs a description, and all 11 already have `brief` set.

### 6.11 Aliases are silently lost

App commands have no alias mechanism. 12 commands have aliases, including
heavily-used short ones:

`gg`, `gitgudders`, `gitbadders` (`gudgitters`) · `mgg`, `monthlygg`,
`monthlygitgudders`, `monthlygitbadders` (`monthlygudgitters`) · `probrat`
(`problemratings`) · `perf` (`plot performance`) · `chilli` (`plot scatter`) ·
`gitbad` · `gotbad` · `toobad` · `link`/`unlink` · `vcperf` · `versushistory`.

They keep working on the (mention-only) prefix path but will not exist as slash
commands.

**Fix if wanted:** register the popular ones as additional top-level hybrid
commands that delegate. We have headroom — 37 of 100 top-level slots used.
**Decision needed:** which aliases are worth a slot. Recommend `gg` and `mgg`
at most.

### 6.12 No test suite on this branch

`git ls-files tests` returns nothing on `master` — the `tests/` directories
contain only stale `__pycache__`. There is no automated safety net for a
118-command refactor.

**Mitigation:** write a standalone smoke script (see §7) before starting
Stage 2. This is cheap and catches the entire class of "Discord rejects the
command tree" errors without connecting to Discord.

---

## 7. Verification

1. **Offline tree build (do this first).** A script that instantiates the bot,
   loads every cog, and walks `bot.tree.get_commands()` without connecting.
   Catches: over-long descriptions (§6.10), invalid names (§6.9), unsupported
   parameter types (§6.2, §6.3), and duplicate names. Run it after every cog
   conversion.
2. **Test guild.** Invite a second bot application with the intent **off** in
   the Developer Portal — this is the only way to actually prove the migration,
   since a locally-disabled intent flag is not the same as a portal-disabled one.
3. **Manual matrix**, per converted cog: invoke as `/cmd`, as `@TLE cmd`, and as
   `;cmd` (the last must *not* respond).
4. **Slow-command check:** `/plot rating`, `/stalk`, `/ranklist` — confirm the
   defer lands and no "did not respond" appears.
5. **Paginated output:** `/duel history`, `/handle list` — confirm the first page
   answers the interaction and the page controls work.
6. **Starboard:** star a message, confirm the agreed degraded entry appears.
7. **Error path:** trigger an unhandled exception, confirm the log channel entry
   is readable and does not crash on a missing `jump_url`.

---

## 8. Open decisions

These block specific stages and need an answer before that stage starts:

1. **Starboard** (§6.7) — link-only entries, or remove the cog?
2. **`ratedvc` / `remind here`** (§6.3) — string parameter, or leave prefix-only?
3. **Lockout wizard** (§6.8) — signature parameters, or modal?
4. **Aliases** (§6.11) — which, if any, get their own slash command?
5. **Renames** (§6.9) — confirm `_nogud` → `forceskip`, `duel _invalidate` →
   `forceinvalidate`, and accept the prefix-name break.
6. **Admin command visibility** (§6.6) — hide with `default_permissions`, or
   accept that they show up for everyone?

---

## Out of scope

**The `SERVER MEMBERS` intent.** It is a separate and largely independent piece
of work, touching `guild.get_member()` / `guild.members` / `bot.get_all_members()`
across `duel`, `handles`, `lockout`, `contests`, `training`, `graphs`, plus
`on_member_join` / `on_member_remove`, which stop firing entirely. It also has a
different shape of fix (REST fallbacks and periodic reconciliation rather than a
command-surface rewrite). It gets its own plan once phase 1 lands.
