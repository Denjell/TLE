# Migration: running TLE without the Server Members intent

Branch: `NoServerPresenceIntent` (branched from `NoPrivilegedIntents` @ `9eb155a`)

This is phase 2, after [NoMessageContentIntent.md](NoMessageContentIntent.md)
(phase 1, Message Content). With it, TLE requests **no privileged intents at
all**: `intents.members = True` is gone from [tle/\_\_main\_\_.py](tle/__main__.py),
and Presence was never requested (`Intents.default()` leaves it out).

> **Status (2026-10-02):** the code is complete and passes the offline checks
> (§7). It has **not** yet been run live with the intent switched off in the
> Developer Portal. That live checklist is the remaining step.

## 1. What the intent gates

Checked against discord.py 2.7.1:

- **The member cache is all but empty.** `MemberCacheFlags.joined` needs the
  intent and guilds are not chunked at startup, so `guild.get_member(id)`
  returns `None` for nearly everyone and `guild.members` /
  `bot.get_all_members()` are near-empty.
- **`on_member_join` / `on_member_remove` never fire.**
- **`guild.fetch_members()` and `guild.chunk()` raise.**

Still available without it:

- `ctx.author` — messages and interactions carry the author's member data.
- Slash `discord.Member` options — resolved by Discord.
- `guild.query_members(user_ids=[…])` over the gateway, up to 100 ids a request.
  Discord only needs the intent to list the *whole* guild, and discord.py only
  guards `presences=True`. `MemberConverter` falls back to this, so
  `resolve_handles` and `!name` lookups keep working.
- `guild.fetch_member(id)` over REST.

## 2. The replacement primitives

All in [tle/util/discord_common.py](tle/util/discord_common.py):

| Helper | Use it for |
|---|---|
| `fetch_members(guild, ids) -> {id: Member}` | Anything needing Member data (names, roles). Cache first, then one gateway request per 100 ids. An id that's missing = not in the guild. Results are **not** cached, because nothing would evict a member who left. |
| `fetch_member(guild, id)` | The one-id form, `None` if absent. |
| `fetch_member_or_user(bot, guild, id)` | Records that outlive membership (duels). Returns a `discord.User` for someone who left; both types have `id`, `mention`, `display_name`. |
| `mention(id)` | When only a mention is needed. `<@id>` renders client side and needs no member data. |

**Membership** is answered by the DB's `user_handle.active` flag, which
`get_handles_for_guild` / `get_cf_users_for_guild` already filter on. It's no
longer inferred from `get_member(...) is None`.

## 3. Keeping `active` correct without events

`on_member_join` / `on_member_remove` are deleted. `Handles._reconcile_status`
([tle/cogs/handles.py](tle/cogs/handles.py)) replaces them:

- It reads every linked user of the guild, active or not, through the new
  `user_db.get_all_handles_for_guild`.
- It looks them up with `fetch_members`.
- It marks leavers inactive and people who rejoined active, then reassigns the
  rank role for those who came back (what `on_member_join` used to do).

It runs from the `SetExUsersInactive` task. That task's interval dropped from
6 h to 1 h, because it's now the only signal. `/updatestatus` runs it
immediately for the current guild instead of reading `guild.members`.

The trade-off: a join or leave is noticed within an hour, not instantly.

## 4. Call sites changed

- **handles**: `list`, `pretty`, `rget`, `remove`, `unmagic_all`,
  `gitgudders`, `monthlygitgudders` (membership via `active`, names via one
  batched lookup of the ≤20 shown), `_update_ranks`, `_make_rankup_embeds`
  (now `async`, fetches only members with a rating change), `updatestatus`,
  the status task.
- **duel**: every challenger/challengee lookup goes through
  `fetch_member_or_user`. `challenge` uses `ctx.author` / `opponent` directly.
  `ranklist` and the rating plot do one batched lookup. `invalidate` uses
  `mention(id)`. This also fixes a pre-existing crash when a duelist had left
  the server.
- **lockout**: mentions via `mention(id)`. The ELO calculation only needs ids,
  so it gets `discord.Object`s (a player who left no longer crashes the round's
  end).
- **contests**: the ratedvc busy-members error and the VC results embed use
  `mention(id)`. `vcratings` makes one batched lookup instead of one
  `MemberConverter` round trip per user.
- **graphs** `distrib`, **training** `fastest`: one batched lookup each.
- **discord_common** `presence`: the "<name> orz" status picks from linked,
  active users instead of `bot.get_all_members()`, which is empty now and would
  have raised on `random.choice`.

## 5. Costs and limits

Gateway member requests share the 120-events-per-60 s gateway budget. At 100
ids per request, a guild with 1000 linked handles costs about 10 requests for a
full `/handle list`, `/handle pretty`, `/roleupdate now` or reconciliation.

## 6. Leftovers

- `user_db.reset_status` has no callers any more.
- The `is None` warnings in `Dueling._check_ongoing_duels_for_guild` can no
  longer trigger.

## 7. Verification

- `python extra/check_app_commands.py` → no problems (all cogs load).
- `grep -rn "get_member\|guild.members\|get_all_members\|on_member_" tle/`
  → only the fast path inside `fetch_members`.
- Live, in the dev guild with **Server Members Intent switched off** in the
  Developer Portal:
  - `/handle list`, `/handle pretty`, `/handle rget`, `/handle remove`
  - `/updatestatus`, `/roleupdate now`, `/plot distrib`
  - `/gitgudders`, `/monthlygitgudders`, `/vcratings`, `/training fastest`
  - a full duel (challenge → accept → complete / draw)
  - a lockout round to completion
  - with an alt account: leave, then `/updatestatus` marks it inactive; rejoin,
    then `/updatestatus` reactivates it and reassigns the rank role.
