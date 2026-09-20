import datetime
import random
from typing import List, Literal, Optional, get_args
import math
import time
from collections import defaultdict
import logging

import discord
from discord import app_commands
from discord.ext import commands


from tle import constants
from tle.util import codeforces_api as cf
from tle.util import codeforces_common as cf_common
from tle.util import discord_common
from tle.util.db.user_db_conn import Gitgud
from tle.util import paginator
from tle.util import cache_system2


# Divisions are kept separate from ordinary tags because asking for a division
# costs none of the points that asking for a tag does. TLE writes these into
# Problem.tags itself when caching problemsets, so they have to be kept out of
# anything that offers "real" Codeforces tags.
_Division = Literal['div1', 'div2', 'div3', 'div4', 'edu']

if set(get_args(_Division)) != set(cache_system2._DIV_TAGS):
    raise RuntimeError('_Division has drifted from cache_system2._DIV_TAGS: '
                       f'{get_args(_Division)} vs {cache_system2._DIV_TAGS}')

# Codeforces participant types, under the names the slash options offer.
_SUBMISSION_TYPES = {
    'contest': 'CONTESTANT',
    'out_of_competition': 'OUT_OF_COMPETITION',
    'virtual': 'VIRTUAL',
    'practice': 'PRACTICE',
}

# Contest name patterns worth offering for /vc. vc matches any substring of a
# contest name, so this is a shortlist rather than a closed set; each of these
# matches contests in the cache today.
_VC_PATTERNS = ('div1', 'div2', 'div3', 'div4', 'educational', 'global',
                'icpc', 'technocup', 'kotlin', 'hello', 'goodbye',
                'april fools', 'vk cup', 'bubble cup', 'codeton')

# Date options are read as bare digits, by length. Both orders are offered:
# day-first is what the 'd>=' prefix syntax has always taken, year-first is what
# a labelled field invites.
_DATE_SEPARATORS = str.maketrans('', '', '-/. ')
_DATE_FORMATS = {4: ('%Y',), 6: ('%m%Y', '%Y%m'), 8: ('%d%m%Y', '%Y%m%d')}

_GITGUD_NO_SKIP_TIME = 2 * 60 * 60
_GITGUD_SCORE_DISTRIB = (1, 2, 3, 5, 8, 12, 17, 23)
_GITGUD_SCORE_DISTRIB_MIN = -400
_GITGUD_SCORE_DISTRIB_MAX =  300
_ONE_WEEK_DURATION = 7 * 24 * 60 * 60
_GITGUD_MORE_POINTS_START_TIME = 1680300000

def _calculateGitgudScoreForDelta(delta):
    if (delta <= _GITGUD_SCORE_DISTRIB_MIN):
        return _GITGUD_SCORE_DISTRIB[0]
    if (delta >= _GITGUD_SCORE_DISTRIB_MAX):
        return _GITGUD_SCORE_DISTRIB[-1]
    index = (delta - _GITGUD_SCORE_DISTRIB_MIN)//100
    return _GITGUD_SCORE_DISTRIB[index]

class CodeforcesCogError(commands.CommandError):
    pass


class Codeforces(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.converter = commands.MemberConverter()
        self.logger = logging.getLogger(self.__class__.__name__)

    # more points seasons start at April 1st 2023 (timestamp: 1680300000) and is only active in the last 7 days of the month

    # @@@ add issue and finish time constraint (both times need to be within the more points range)
    def _check_more_points_active(self, now_time, start_time, end_time):
        morePointsActive = False
        morePointsTime = end_time - _ONE_WEEK_DURATION
        if start_time >= _GITGUD_MORE_POINTS_START_TIME and now_time >= morePointsTime: 
            morePointsActive = True
        return morePointsActive

    async def _validate_gitgud_status(self, ctx):
        user_id = ctx.message.author.id
        active = cf_common.user_db.check_challenge(user_id)
        if active is not None:
            _, _, name, contest_id, index, _ = active
            url = f'{cf.CONTEST_BASE_URL}{contest_id}/problem/{index}'
            raise CodeforcesCogError(f'You have an active challenge {name} at {url}')

    async def _gitgud(self, ctx, handle, problem, delta, hidden):
        # The caller of this function is responsible for calling `_validate_gitgud_status` first.
        user_id = ctx.author.id

        issue_time = datetime.datetime.now().timestamp()
        rc = cf_common.user_db.new_challenge(user_id, issue_time, problem, delta)
        if rc != 1:
            raise CodeforcesCogError('Your challenge has already been added to the database!')

        # Calculate time range of given month (d=) or current month
        now = datetime.datetime.now()
        start_time, end_time = cf_common.get_start_and_end_of_month(now)
        now_time = int(now.timestamp())
        # more points seasons start at April 1st 2023 (timestamp: 1680300000) and is only active in the last 7 days of the month
        morePointsActive = self._check_more_points_active(now_time, start_time, end_time)

        points = _calculateGitgudScoreForDelta(delta)
        monthlypoints = 2 * points if morePointsActive else points

        title = f'{problem.index}. {problem.name}'
        desc = cf_common.cache2.contest_cache.get_contest(problem.contestId).name
        ratingStr = problem.rating if not hidden else '||'+str(problem.rating)+'||'
        pointsStr = points if not hidden else '||'+str(points)+'||'
        monthlyPointsStr = monthlypoints if not hidden else '||'+str(monthlypoints)+'||'
        embed = discord.Embed(title=title, url=problem.url, description=desc)
        embed.add_field(name='Rating', value=ratingStr)
        embed.add_field(name='Alltime points', value=pointsStr)
        embed.add_field(name='Monthly points', value=monthlyPointsStr)
        await ctx.send(f'Challenge problem for `{handle}`', embed=embed)

    @commands.hybrid_command(brief='Upsolve a problem')
    @cf_common.user_guard(group='gitgud')
    async def upsolve(self, ctx, choice: int = -1):
        """Upsolve: The command /upsolve lists all problems that you haven't solved in contests you participated 
        - Type /upsolve for listing all available problems.
        - Type /upsolve <nr> for choosing the problem <nr> as gitgud problem (only possible if you have no active gitgud challenge)
        - After solving the problem you can claim gitgud points for it with /gotgud
        - If you can't solve the problem or used external help you should skip it with /nogud (Available after 2 hours)
        - The all-time ranklist can be found with /gitgudders
        - A monthly ranklist is shown when you type /monthlygitgudders
        - Another way to gather gitgud points is /gitgud (only works if you have no active gitgud-Challenge)
        - For help with each of the commands, mention the bot: @TLE help <command> (e.g. help gitgudders)
        
        Point distribution:
        delta  | <-300| -300 | -200 | -100 |  0  |  100 |  200 |>=300
        points |   1  |   2  |   3  |   5  |  8  |  12  |  17  |  23 
        """
        handle, = await cf_common.resolve_handles(ctx, self.converter, ('!' + str(ctx.author),))
        user = cf_common.user_db.fetch_cf_user(handle)
        rating = round(user.effective_rating, -2)
        rating = max(1100, rating)
        rating = min(3000, rating)
        resp = await cf.user.rating(handle=handle)
        contests = {change.contestId for change in resp}
        submissions = await cf.user.status(handle=handle)
        solved = {sub.problem.name for sub in submissions if sub.verdict == 'OK'}
        problems = [prob for prob in cf_common.cache2.problem_cache.problems
                    if prob.name not in solved and prob.contestId in contests]

        if not problems:
            raise CodeforcesCogError('Problems not found within the search parameters')

        problems.sort(key=lambda problem: problem.rating)

        if choice > 0 and choice <= len(problems):
            await self._validate_gitgud_status(ctx)
            problem = problems[choice - 1]
            await self._gitgud(ctx, handle, problem, problem.rating - rating, False)
        else:
            problems = problems[:500]
              
            def make_line(i, prob):
                data = (f'{i + 1}: [{prob.name}]({prob.url}) [{prob.rating}]')
                return data

            def make_page(chunk, pi, num):
                title = f'Select a problem to upsolve (1-{num}):'
                msg = '\n'.join(make_line(10*pi+i, prob) for i, prob in enumerate(chunk))
                embed = discord_common.cf_color_embed(description=msg)
                return title, embed
                  
            pages = [make_page(chunk, pi, len(problems)) for pi, chunk in enumerate(paginator.chunkify(problems, 10))]
            paginator.paginate(self.bot, ctx.channel, pages, wait_time=5 * 60, set_pagenum_footers=True, ctx=ctx)   

    @staticmethod
    def _complete_list_field(current, known):
        """Autocomplete a comma separated field, preserving what is already typed.

        Discord replaces the whole option value with the chosen suggestion, so
        every choice has to carry the earlier entries along with it.
        """
        head, _, partial = current.rpartition(',')
        head = head.strip()
        partial = partial.strip().lower()
        prefix = f'{head}, ' if head else ''
        choices = []
        for entry in known:
            if partial and partial not in entry.lower():
                continue
            value = f'{prefix}{entry}'
            if len(value) <= 100:
                choices.append(app_commands.Choice(name=value, value=value))
            if len(choices) == 25:
                break
        return choices

    async def _tag_autocomplete(self, interaction, current: str):
        """Suggest Codeforces tags.

        There are more tags than the 25 choice limit allows, which is why this
        is autocomplete rather than a dropdown.
        """
        known = sorted({tag for problem in cf_common.cache2.problem_cache.problems
                        for tag in problem.tags
                        if tag not in cache_system2._DIV_TAGS})
        return self._complete_list_field(current, known)

    async def _submission_type_autocomplete(self, interaction, current: str):
        """Suggest submission types.

        These would fit in a dropdown, but a dropdown holds one value and
        asking for, say, contest and virtual together is a fair thing to want.
        """
        return self._complete_list_field(current, sorted(_SUBMISSION_TYPES))

    async def _contest_pattern_autocomplete(self, interaction, current: str):
        """Suggest contest name patterns for /vc.

        Not a closed set: vc matches any substring of a contest name, so a
        pattern typed by hand works just as well. Deriving the list from the
        contest cache was tried and buries the real series names under words
        like 'rules', 'teams' and 'day'.
        """
        return self._complete_list_field(current, _VC_PATTERNS)

    @staticmethod
    def _split_list(text):
        """Split a comma separated field into its entries."""
        return [entry.strip() for entry in text.split(',') if entry.strip()]

    @classmethod
    def _split_tags(cls, text, field, *, tags_cost_points=False):
        """Split a comma separated tag field.

        Comma rather than space because 14 of the Codeforces tags contain one
        ('binary search', 'data structures'), which the old '+tag' syntax could
        not express at all.

        Division tags are rejected rather than accepted quietly. Every command
        offering this field offers a division parameter too, which filters the
        same way without the gitgud tag penalty.
        """
        tags = cls._split_list(text)
        divisions = [tag for tag in tags if tag in cache_system2._DIV_TAGS]
        if divisions:
            message = f'`{", ".join(divisions)}` belongs in the division option, not `{field}`.'
            if tags_cost_points:
                message += ' Filtering by division there costs no points, while a tag costs 200.'
            raise CodeforcesCogError(message)
        return tags

    @classmethod
    def _split_types(cls, text):
        """Split a comma separated submission type field into API values."""
        names = [name.lower() for name in cls._split_list(text)]
        unknown = [name for name in names if name not in _SUBMISSION_TYPES]
        if unknown:
            raise CodeforcesCogError(
                f'`{", ".join(unknown)}` is not a submission type. '
                f'Pick from `{", ".join(sorted(_SUBMISSION_TYPES))}`.')
        # An empty field means every type, which is what SubFilter.parse does.
        return [_SUBMISSION_TYPES[name] for name in names] or list(_SUBMISSION_TYPES.values())

    @staticmethod
    def _parse_date_option(text, field):
        """Parse a date option into a timestamp.

        cf_common.parse_date takes a bare ddmmyyyy, mmyyyy or yyyy, which reads
        fine behind a 'd>=' prefix but looks like a typo in a labelled field.
        Separators are dropped, and year-first order is accepted alongside the
        day-first order the prefix syntax uses. The two cannot collide for any
        real date: 01032024 is not a year 0103, and 20240301 is not a day 20 of
        a month 24.
        """
        digits = text.translate(_DATE_SEPARATORS)
        for fmt in _DATE_FORMATS.get(len(digits), ()):
            try:
                return time.mktime(datetime.datetime.strptime(digits, fmt).timetuple())
            except ValueError:
                continue
        raise CodeforcesCogError(
            f'`{text}` is not a valid date for `{field}`. Give a year (2024), '
            'a month (2024-03) or a day (2024-03-01).')

    @commands.hybrid_command(brief='Recommend a problem')
    @app_commands.describe(
        rating='Problem rating, 800-3500. Defaults to your own rating.',
        max_rating='Upper bound of a rating range. The rating is then hidden.',
        tags='Only problems with these tags, comma separated.',
        exclude_tags='Never recommend problems with these tags, comma separated.',
        division='Only problems from this division.',
        exclude_division='Never recommend problems from this division.',
        after='Only contests from this date on, as 2024, 2024-03 or 2024-03-01.',
        before='Only contests before this date, as 2024, 2024-03 or 2024-03-01.',
    )
    @app_commands.autocomplete(tags=_tag_autocomplete, exclude_tags=_tag_autocomplete)
    @cf_common.user_guard(group='gitgud')
    async def gimme(self, ctx,
                    rating: Optional[app_commands.Range[int, 800, 3500]] = None,
                    max_rating: Optional[app_commands.Range[int, 800, 3500]] = None,
                    tags: str = '',
                    exclude_tags: str = '',
                    division: Optional[_Division] = None,
                    exclude_division: Optional[_Division] = None,
                    after: Optional[str] = None,
                    before: Optional[str] = None):
        """Recommend a problem you have not solved yet.

        Without a rating you get one at your own rating, rounded to the nearest
        100. Giving `max_rating` as well asks for a range instead, and hides the
        problem rating behind a spoiler so the range does not give the
        difficulty away.

        Unlike /gitgud this earns no points and starts no challenge, so nothing
        here costs anything.
        """
        handle, = await cf_common.resolve_handles(ctx, self.converter, ('!' + str(ctx.author),))
        user_rating = round(cf_common.user_db.fetch_cf_user(handle).effective_rating, -2)

        if max_rating is not None and rating is None:
            raise CodeforcesCogError('Give `rating` as the lower bound when using `max_rating`.')
        srating = user_rating if rating is None else rating
        erating = srating if max_rating is None else max_rating
        if erating < srating:
            raise CodeforcesCogError(f'`max_rating` ({erating}) is below `rating` ({srating}).')

        tags = self._split_tags(tags, 'tags')
        bantags = self._split_tags(exclude_tags, 'exclude_tags')
        if division is not None:
            tags.append(division)
        if exclude_division is not None:
            bantags.append(exclude_division)

        dlo = 0 if after is None else self._parse_date_option(after, 'after')
        dhi = 10**10 if before is None else self._parse_date_option(before, 'before')

        submissions = await cf.user.status(handle=handle)
        solved = {sub.problem.name for sub in submissions if sub.verdict == 'OK'}

        problems = [prob for prob in cf_common.cache2.problem_cache.problems
                    if prob.rating >= srating and prob.rating <= erating and prob.name not in solved
                    and not cf_common.is_contest_writer(prob.contestId, handle)
                    and prob.matches_all_tags(tags)
                    and not prob.matches_any_tag(bantags)
                    and dlo <= cf_common.cache2.contest_cache.get_contest(prob.contestId).startTimeSeconds < dhi]

        if not problems:
            raise CodeforcesCogError('Problems not found within the search parameters')

        problems.sort(key=lambda problem: cf_common.cache2.contest_cache.get_contest(
            problem.contestId).startTimeSeconds)

        choice = max([random.randrange(len(problems)) for _ in range(3)])
        problem = problems[choice]

        title = f'{problem.index}. {problem.name}'
        desc = cf_common.cache2.contest_cache.get_contest(problem.contestId).name
        embed = discord.Embed(title=title, url=problem.url, description=desc)
        ratingStr = problem.rating if srating == erating else '||'+str(problem.rating)+'||'
        embed.add_field(name='Rating', value=ratingStr)
        if tags:
            tagslist = ', '.join(problem.get_matched_tags(tags))
            embed.add_field(name='Matched tags', value=tagslist)
        await ctx.send(f'Recommended problem for `{handle}`', embed=embed)

    @commands.hybrid_command(brief='List solved problems')
    @app_commands.describe(
        handles='Codeforces handles or Discord mentions, space separated. Defaults to you.',
        sort='Most recently solved first (default), or hardest first.',
        tags='Only problems with these tags, comma separated.',
        exclude_tags='Skip problems with these tags, comma separated.',
        division='Only problems from this division.',
        exclude_division='Skip problems from this division.',
        min_rating='Only problems rated at least this.',
        max_rating='Only problems rated at most this.',
        after='Only submissions from this date on, as 2024, 2024-03 or 2024-03-01.',
        before='Only submissions before this date, as 2024, 2024-03 or 2024-03-01.',
        types='Submission types, comma separated. All of them by default.',
        contests='Only these contests, comma separated, matched against the contest name.',
        indices='Only these problem indices, comma separated, such as A or C1.',
        include_team='Include problems solved as part of a team.',
    )
    @app_commands.autocomplete(tags=_tag_autocomplete, exclude_tags=_tag_autocomplete,
                               types=_submission_type_autocomplete)
    async def stalk(self, ctx, handles: str = '',
                    sort: Literal['recent', 'hardest'] = 'recent',
                    tags: str = '',
                    exclude_tags: str = '',
                    division: Optional[_Division] = None,
                    exclude_division: Optional[_Division] = None,
                    min_rating: Optional[app_commands.Range[int, 500, 3800]] = None,
                    max_rating: Optional[app_commands.Range[int, 500, 3800]] = None,
                    after: Optional[str] = None,
                    before: Optional[str] = None,
                    types: str = '',
                    contests: str = '',
                    indices: str = '',
                    include_team: bool = False):
        """Print problems solved by a user, most recent first.

        Every submission type is included unless `types` says otherwise, so
        practice counts the same as a contest. A problem solved more than once
        is listed once, at its first accepted submission.
        """
        hardest = sort == 'hardest'

        # Built by hand rather than through SubFilter.parse, which exists to
        # read the old '+tag r>=1500' string syntax back out of a message.
        filt = cf_common.SubFilter(False)
        filt.tags = self._split_tags(tags, 'tags')
        filt.bantags = self._split_tags(exclude_tags, 'exclude_tags')
        if division is not None:
            filt.tags.append(division)
        if exclude_division is not None:
            filt.bantags.append(exclude_division)
        if min_rating is not None:
            filt.rlo = min_rating
        if max_rating is not None:
            filt.rhi = max_rating
        # An unrated problem can satisfy no rating bound, so asking for one
        # turns the rated flag on, as SubFilter.parse does.
        filt.rated = min_rating is not None or max_rating is not None
        if after is not None:
            filt.dlo = self._parse_date_option(after, 'after')
        if before is not None:
            filt.dhi = self._parse_date_option(before, 'before')
        filt.types = self._split_types(types)
        filt.contests = self._split_list(contests)
        filt.indices = self._split_list(indices)
        filt.team = include_team

        handles = handles.split() or ('!' + str(ctx.author),)
        handles = await cf_common.resolve_handles(ctx, self.converter, handles)
        submissions = [await cf.user.status(handle=handle) for handle in handles]
        submissions = [sub for subs in submissions for sub in subs]
        submissions = filt.filter_subs(submissions)

        if not submissions:
            raise CodeforcesCogError('Submissions not found within the search parameters')

        if hardest:
            submissions.sort(key=lambda sub: (sub.problem.rating or 0, sub.creationTimeSeconds), reverse=True)
        else:
            submissions.sort(key=lambda sub: sub.creationTimeSeconds, reverse=True)

        handlesWithUrl = ['`{}` (https://codeforces.com/profile/{})'.format(handle,handle) for handle in handles]

        def make_line(sub):
            data = (f'[{sub.problem.name}]({sub.problem.url})',
                    f'[{sub.problem.rating if sub.problem.rating else "?"}]',
                    f'({cf_common.days_ago(sub.creationTimeSeconds)})')
            return '\N{EN SPACE}'.join(data)

        def make_page(chunk):
            title = '{} solved problems by {}'.format('Hardest' if hardest else 'Recently',
                                                        ', '.join(handlesWithUrl))
            hist_str = '\n'.join(make_line(sub) for sub in chunk)
            embed = discord_common.cf_color_embed(description=hist_str)
            return title, embed

        pages = [make_page(chunk) for chunk in paginator.chunkify(submissions[:100], 10)]
        paginator.paginate(self.bot, ctx.channel, pages, wait_time=5 * 60, set_pagenum_footers=True, ctx=ctx)

    @commands.hybrid_command(brief='Create a mashup', usage='[handles] [+tag..] [~tag..] [+divX] [~divX] [?[-]delta]')
    async def mashup(self, ctx, *, args: str = ''):
        """Create a mashup contest using problems within -200 and +400 of average rating of handles provided.
        Add tags with "+" before them.
        Ban tags with "~" before them.
        """
        # Slash commands have no variadic parameter, so the handles and
        # filters arrive as one field. Prefix invocations are unaffected.
        args = args.split()
        delta = 100
        handles = [arg for arg in args if arg[0] not in '+~?']
        tags = cf_common.parse_tags(args, prefix='+')
        bantags = cf_common.parse_tags(args, prefix='~')
        deltaStr = [arg[1:] for arg in args if arg[0] == '?' and len(arg) > 1]
        if len(deltaStr) > 1:
            raise CodeforcesCogError('Only one delta argument is allowed')
        if len(deltaStr) == 1:
            try:
                delta += round(int(deltaStr[0]), -2)
            except ValueError:
                raise CodeforcesCogError('delta could not be interpreted as number')

        handles = handles or ('!' + str(ctx.author),)
        handles = await cf_common.resolve_handles(ctx, self.converter, handles)
        resp = [await cf.user.status(handle=handle) for handle in handles]
        submissions = [sub for user in resp for sub in user]
        solved = {sub.problem.name for sub in submissions}
        info = await cf.user.info(handles=handles)
        rating = int(round(sum(user.effective_rating for user in info) / len(handles), -2))
        rating += delta
        rating = max(800, rating)
        rating = min(3500, rating)
        problems = [prob for prob in cf_common.cache2.problem_cache.problems
                    if abs(prob.rating - rating) <= 300 and prob.name not in solved
                    and not any(cf_common.is_contest_writer(prob.contestId, handle) for handle in handles)
                    and not cf_common.is_nonstandard_problem(prob)
                    and prob.matches_all_tags(tags)
                    and not prob.matches_any_tag(bantags)]

        if len(problems) < 4:
            raise CodeforcesCogError('Problems not found within the search parameters')

        problems.sort(key=lambda problem: cf_common.cache2.contest_cache.get_contest(
            problem.contestId).startTimeSeconds)

        choices = []
        for i in range(4):
            k = max(random.randrange(len(problems) - i) for _ in range(2))
            for c in choices:
                if k >= c:
                    k += 1
            choices.append(k)
            choices.sort()

        problems = sorted([problems[k] for k in choices], key=lambda problem: problem.rating)
        msg = '\n'.join(f'{"ABCD"[i]}: [{p.name}]({p.url}) [{p.rating}]' for i, p in enumerate(problems))
        str_handles = '`, `'.join(handles)
        embed = discord_common.cf_color_embed(description=msg)
        await ctx.send(f'Mashup contest for `{str_handles}`', embed=embed)

    @commands.hybrid_command(brief='Request a problem to solve for gitgud points',
                             aliases=['gitbad'])
    @app_commands.describe(
        rating='Problem rating, 800-3500. Defaults to your own rating.',
        max_rating='Upper bound of a rating range. The rating and points are then hidden.',
        tags='Only problems with these tags, comma separated. Costs 200 points.',
        exclude_tags='Never pick problems with these tags, comma separated.',
        division='Only problems from this division. Costs no points.',
        exclude_division='Never pick problems from this division.',
    )
    @app_commands.autocomplete(tags=_tag_autocomplete, exclude_tags=_tag_autocomplete)
    @cf_common.user_guard(group='gitgud')
    async def gitgud(self, ctx,
                     rating: Optional[app_commands.Range[int, 800, 3500]] = None,
                     max_rating: Optional[app_commands.Range[int, 800, 3500]] = None,
                     tags: str = '',
                     exclude_tags: str = '',
                     division: Optional[_Division] = None,
                     exclude_division: Optional[_Division] = None):
        """Request a problem to solve for gitgud points.

        Points are assigned by the difference between the problem rating and
        your own rating, rounded to the nearest 100. Asking for tags costs 200
        points; asking for a division costs nothing.

        Giving `max_rating` asks for a range instead of a single rating, and
        hides the problem rating and the points behind a spoiler so the range
        does not give away the difficulty.

        - Claim your points once solved with /gotgud
        - Skip a problem you cannot solve with /nogud (available after 2 hours)
        - Ranklists: /gitgudders and /monthlygitgudders
        - /upsolve is another way to gather points, when no challenge is active

        Point distribution:
        rating diff | <-300| -300 | -200 | -100 |   0  |  100 |  200 |>=300
        no tags     |   1  |   2  |   3  |   5  |   8  |  12  |  17  |  23 
        rating diff | <-100| -100 |   0  |  100 |  200 |  300 |  400 |>=500
        tags        |   1  |   2  |   3  |   5  |   8  |  12  |  17  |  23 
        """
        handle, = await cf_common.resolve_handles(ctx, self.converter, ('!' + str(ctx.author),))
        user = cf_common.user_db.fetch_cf_user(handle)
        user_rating = round(user.effective_rating, -2)
        user_rating = max(800, user_rating)
        user_rating = min(3500, user_rating)
        # Points are scored against the requester's own rating, never against
        # the rating they asked for.
        scoring_rating = min(3000, max(1100, user_rating))

        if max_rating is not None and rating is None:
            raise CodeforcesCogError('Give `rating` as the lower bound when using `max_rating`.')
        srating = user_rating if rating is None else rating
        erating = srating if max_rating is None else max_rating
        if erating < srating:
            raise CodeforcesCogError(f'`max_rating` ({erating}) is below `rating` ({srating}).')
        # A range gives away less about the difficulty, so the rating and the
        # points are spoilered when one is asked for.
        hidden = max_rating is not None

        submissions = await cf.user.status(handle=handle)
        solved = {sub.problem.name for sub in submissions}
        noguds = cf_common.user_db.get_noguds(ctx.author.id)

        tags = self._split_tags(tags, 'tags', tags_cost_points=True)
        bantags = self._split_tags(exclude_tags, 'exclude_tags', tags_cost_points=True)
        # Divisions are appended after this, and must not count towards the
        # tag penalty.
        scored_as_tagged = bool(tags or bantags)
        if division is not None:
            tags.append(division)
        if exclude_division is not None:
            bantags.append(exclude_division)

        await self._validate_gitgud_status(ctx)

        problems = [prob for prob in cf_common.cache2.problem_cache.problems
                    if prob.rating >= srating and prob.rating <= erating
                    and prob.name not in solved 
                    and prob.name not in noguds
                    and prob.matches_all_tags(tags)
                    and not prob.matches_any_tag(bantags)]
                        

        def check(problem):
            return (not cf_common.is_nonstandard_problem(problem) and
                    not cf_common.is_contest_writer(problem.contestId, handle))

        problems = list(filter(check, problems))
        if not problems:
            raise CodeforcesCogError('No problem to assign')

        problems.sort(key=lambda problem: cf_common.cache2.contest_cache.get_contest(
            problem.contestId).startTimeSeconds)

        choice = max(random.randrange(len(problems)) for _ in range(5))

        delta = problems[choice].rating - scoring_rating
        # Divisions never reduce the score, only real tags do. That used to be
        # done by filtering _DIV_TAGS back out of the tag lists here; the
        # division is a separate parameter now, so it was never mixed in.
        if scored_as_tagged:
            delta = delta - 200
        await self._gitgud(ctx, handle, problems[choice], delta, hidden)

    @commands.hybrid_command(brief='Print user gitgud history')
    async def gitlog(self, ctx, member: discord.Member = None):
        """Displays the list of gitgud problems issued to the specified member, excluding those noguded by admins.
        If the challenge was completed, time of completion and amount of points gained will also be displayed.
        """
        def make_line(entry):
            issue, finish, name, contest, index, delta, status = entry
            problem = cf_common.cache2.problem_cache.problem_by_name[name]
            line = f'[{name}]({problem.url})\N{EN SPACE}[{problem.rating}]'
            if finish:
                time_str = cf_common.days_ago(finish)
                points = f'{_calculateGitgudScoreForDelta(delta):+}'
                line += f'\N{EN SPACE}{time_str}\N{EN SPACE}[{points}]'
            return line

        def make_page(chunk,score):
            message = discord.utils.escape_mentions(f'Gitgud log for {member.display_name} (total score: {score})')
            log_str = '\n'.join(make_line(entry) for entry in chunk)
            embed = discord_common.cf_color_embed(description=log_str)
            return message, embed

        member = member or ctx.author
        data = cf_common.user_db.gitlog(member.id)
        if not data:
            raise CodeforcesCogError(f'{member.mention} has no gitgud history.')
        score = 0
        for entry in data:
            issue, finish, name, contest, index, delta, status = entry
            if finish:
                score+=_calculateGitgudScoreForDelta(delta)
     

        pages = [make_page(chunk, score) for chunk in paginator.chunkify(data, 10)]
        paginator.paginate(self.bot, ctx.channel, pages, wait_time=5 * 60, set_pagenum_footers=True, ctx=ctx)

    @commands.hybrid_command(brief='Print user nogud history')
    async def nogudlog(self, ctx, member: discord.Member = None):
        """Displays the list of nogud problems issued to the specified member, excluding those noguded by admins.
        """
        def make_line(entry):
            issue, finish, name, contest, index, delta, status = entry
            problem = cf_common.cache2.problem_cache.problem_by_name[name]
            line = f'[{name}]({problem.url})\N{EN SPACE}[{problem.rating}]'
            if finish:
                time_str = cf_common.days_ago(finish)
                points = f'{_calculateGitgudScoreForDelta(delta):+}'
                line += f'\N{EN SPACE}{time_str}\N{EN SPACE}[{points}]'
            return line

        def make_page(chunk):
            message = discord.utils.escape_mentions(f'Nogud log for {member.display_name}')
            log_str = '\n'.join(make_line(entry) for entry in chunk)
            embed = discord_common.cf_color_embed(description=log_str)
            return message, embed

        member = member or ctx.author
        data = cf_common.user_db.gitlog(member.id)
        if not data:
            raise CodeforcesCogError(f'{member.mention} has no gitgud history.')

        data = [entry for entry in data if entry[1] is None]                

        pages = [make_page(chunk) for chunk in paginator.chunkify(data, 10)]
        paginator.paginate(self.bot, ctx.channel, pages, wait_time=5 * 60, set_pagenum_footers=True, ctx=ctx)

    @commands.hybrid_command(brief='Report challenge completion', aliases=['gotbad'])
    @cf_common.user_guard(group='gitgud')
    async def gotgud(self, ctx):
        handle, = await cf_common.resolve_handles(ctx, self.converter, ('!' + str(ctx.author),))
        user_id = ctx.message.author.id
        active = cf_common.user_db.check_challenge(user_id)
        if not active:
            raise CodeforcesCogError(f'You do not have an active challenge')

        submissions = await cf.user.status(handle=handle)
        solved = {sub.problem.name for sub in submissions if sub.verdict == 'OK'}

        challenge_id, issue_time, name, contestId, index, delta = active
        if not name in solved:
            raise CodeforcesCogError('You haven\'t completed your challenge.')

        score = _calculateGitgudScoreForDelta(delta)
        finish_time = int(datetime.datetime.now().timestamp())
        rc = cf_common.user_db.complete_challenge(user_id, challenge_id, finish_time, score)

        now = datetime.datetime.now()
        start_time, end_time = cf_common.get_start_and_end_of_month(now)
        now_time = int(now.timestamp())

        morePointsActive = self._check_more_points_active(now_time, start_time, end_time)
        
        monthlyPoints = 2 * score if morePointsActive else score

        if rc == 1:
            duration = cf_common.pretty_time_format(finish_time - issue_time)
            await ctx.send(f'Challenge completed in {duration}. {handle} gained {score} alltime ranklist points and {monthlyPoints} monthly ranklist points.')
        else:
            await ctx.send('You have already claimed your points')

    @commands.hybrid_command(brief='Skip challenge', aliases=['toobad'])
    @cf_common.user_guard(group='gitgud')
    async def nogud(self, ctx):
        await cf_common.resolve_handles(ctx, self.converter, ('!' + str(ctx.author),))
        user_id = ctx.message.author.id
        active = cf_common.user_db.check_challenge(user_id)
        if not active:
            raise CodeforcesCogError(f'You do not have an active challenge')

        challenge_id, issue_time, name, contestId, index, delta = active
        finish_time = int(datetime.datetime.now().timestamp())
        if finish_time - issue_time < _GITGUD_NO_SKIP_TIME:
            skip_time = cf_common.pretty_time_format(issue_time + _GITGUD_NO_SKIP_TIME - finish_time)
            await ctx.send(f'Think more. You can skip your challenge in {skip_time}.')
            return
        cf_common.user_db.skip_challenge(user_id, challenge_id, Gitgud.NOGUD)
        await ctx.send(f'Challenge skipped.')

    @commands.hybrid_command(name='forceskip', aliases=['_nogud'],
                      brief="Force skip another user's challenge")
    @cf_common.user_guard(group='gitgud')
    @commands.has_any_role(constants.TLE_ADMIN, constants.TLE_MODERATOR)
    async def forceskip(self, ctx, member: discord.Member):
        active = cf_common.user_db.check_challenge(member.id)
        if not active:
            await ctx.send(f'No active challenge found for user `{member.display_name}`.')
            return
        rc = cf_common.user_db.skip_challenge(member.id, active[0], Gitgud.FORCED_NOGUD)
        if rc == 1:
            await ctx.send(f'Challenge skip forced.')
        else:
            await ctx.send(f'Failed to force challenge skip.')

    @commands.hybrid_command(brief='Recommend a contest')
    @app_commands.describe(
        handles='Codeforces handles or Discord mentions, space separated. Defaults to you.',
        patterns='Contest name patterns, comma separated. Picked from your rating if empty.',
    )
    @app_commands.autocomplete(patterns=_contest_pattern_autocomplete)
    async def vc(self, ctx, handles: str = '', patterns: str = ''):
        """Recommend a contest nobody in the list has submitted to.

        A pattern matches anywhere in the contest name, ignoring case and
        punctuation, so `div2` finds "(Div. 2)" and `goodbye` finds
        "Good Bye 2023". The suggestions are the usual ones; anything you type
        by hand counts too.

        With no pattern the division follows the average rating: div3 below
        1600, div2 below 2100, and the div1 grade series above that.
        """
        # A leading '+' was how the old single-field syntax told a pattern from
        # a handle. It carries no meaning now, but is cheap to forgive.
        markers = [pattern.lstrip('+') for pattern in self._split_list(patterns)]
        handles = handles.split() or ('!' + str(ctx.author),)
        handles = await cf_common.resolve_handles(ctx, self.converter, handles, maxcnt=25)
        info = await cf.user.info(handles=handles)
        contests = cf_common.cache2.contest_cache.get_contests_in_phase('FINISHED')

        if not markers:
            divr = sum(user.effective_rating for user in info) / len(handles)
            div1_indicators = ['div1', 'global', 'avito', 'goodbye', 'hello']
            markers = ['div3'] if divr < 1600 else ['div2'] if divr < 2100 else div1_indicators

        recommendations = {contest.id for contest in contests if
                           contest.matches(markers) and
                           not cf_common.is_nonstandard_contest(contest) and
                           not any(cf_common.is_contest_writer(contest.id, handle)
                                       for handle in handles)}

        # Discard contests in which user has non-CE submissions.
        visited_contests = await cf_common.get_visited_contests(handles)
        recommendations -= visited_contests

        if not recommendations:
            raise CodeforcesCogError('Unable to recommend a contest')

        recommendations = list(recommendations)
        recommendations.sort(key=lambda contest: cf_common.cache2.contest_cache.get_contest(contest).startTimeSeconds, reverse=True)
        contests = [cf_common.cache2.contest_cache.get_contest(contest_id) for contest_id in recommendations[:25]]

        def make_line(c):
            return f'[{c.name}]({c.url}) {cf_common.pretty_time_format(c.durationSeconds)}'

        def make_page(chunk):
            str_handles = '`, `'.join(handles)
            message = f'Recommended contest(s) for `{str_handles}`'
            vc_str = '\n'.join(make_line(contest) for contest in chunk)
            embed = discord_common.cf_color_embed(description=vc_str)
            return message, embed

        pages = [make_page(chunk) for chunk in paginator.chunkify(contests, 5)]
        paginator.paginate(self.bot, ctx.channel, pages, wait_time=5 * 60, set_pagenum_footers=True, ctx=ctx)

    @commands.hybrid_command(brief="Display unsolved rounds closest to completion", usage='[keywords]')
    async def fullsolve(self, ctx, *, args: str = ''):
        """Displays a list of contests, sorted by number of unsolved problems.
        Contest names matching any of the provided tags will be considered. e.g /fullsolve +edu"""
        # Slash commands have no variadic parameter, so the handles and
        # filters arrive as one field. Prefix invocations are unaffected.
        args = args.split()
        handle, = await cf_common.resolve_handles(ctx, self.converter, ('!' + str(ctx.author),))
        tags = [x for x in args if x[0] == '+']

        problem_to_contests = cf_common.cache2.problemset_cache.problem_to_contests
        contests = [contest for contest in cf_common.cache2.contest_cache.get_contests_in_phase('FINISHED')
                    if (not tags or contest.matches(tags)) and not cf_common.is_nonstandard_contest(contest)]

        # subs_by_contest_id contains contest_id mapped to [list of problem.name]
        subs_by_contest_id = defaultdict(set)
        for sub in await cf.user.status(handle=handle):
            if sub.verdict == 'OK':
                try:
                    contest = cf_common.cache2.contest_cache.get_contest(sub.problem.contestId)
                    problem_id = (sub.problem.name, contest.startTimeSeconds)
                    for contestId in problem_to_contests[problem_id]:
                        subs_by_contest_id[contestId].add(sub.problem.name)
                except cache_system2.ContestNotFound:
                    pass

        contest_unsolved_pairs = []
        for contest in contests:
            num_solved = len(subs_by_contest_id[contest.id])
            try:
                num_problems = len(cf_common.cache2.problemset_cache.get_problemset(contest.id))
                if 0 < num_solved < num_problems:
                    contest_unsolved_pairs.append((contest, num_solved, num_problems))
            except cache_system2.ProblemsetNotCached:
                # In case of recent contents or cetain bugged contests
                pass

        contest_unsolved_pairs.sort(key=lambda p: (p[2] - p[1], -p[0].startTimeSeconds))

        if not contest_unsolved_pairs:
            raise CodeforcesCogError(f'`{handle}` has no contests to fullsolve :confetti_ball:')

        def make_line(entry):
            contest, solved, total = entry
            return f'[{contest.name}]({contest.url})\N{EN SPACE}[{solved}/{total}]'

        def make_page(chunk):
            message = f'Fullsolve list for `{handle}`'
            full_solve_list = '\n'.join(make_line(entry) for entry in chunk)
            embed = discord_common.cf_color_embed(description=full_solve_list)
            return message, embed

        pages = [make_page(chunk) for chunk in paginator.chunkify(contest_unsolved_pairs, 10)]
        paginator.paginate(self.bot, ctx.channel, pages, wait_time=5 * 60, set_pagenum_footers=True, ctx=ctx)

    @staticmethod
    def getEloWinProbability(ra: float, rb: float) -> float:
        return 1.0 / (1 + 10**((rb - ra) / 400.0))

    @staticmethod
    def composeRatings(left: float, right: float, ratings: List[float]) -> int:
        for tt in range(20):
            r = (left + right) / 2.0

            rWinsProbability = 1.0
            for rating, count in ratings:
                rWinsProbability *= Codeforces.getEloWinProbability(r, rating)**count

            if rWinsProbability < 0.5:
                left = r
            else:
                right = r
        return round((left + right) / 2)

    @commands.hybrid_command(brief='Calculate team rating', usage='[handles] [+peak]')
    async def teamrate(self, ctx, *, args: str = ''):
        """Provides the combined rating of the entire team.
        If +server is provided as the only handle, will display the rating of the entire server.
        Supports multipliers. e.g: /teamrate gamegame*1000"""
        # Slash commands have no variadic parameter, so the handles and
        # filters arrive as one field. Prefix invocations are unaffected.
        args = args.split()

        (is_entire_server, peak), handles = cf_common.filter_flags(args, ['+server', '+peak'])
        handles = handles or ('!' + str(ctx.author),)

        def rating(user):
            return user.maxRating if peak else user.rating

        if is_entire_server:
            res = cf_common.user_db.get_cf_users_for_guild(ctx.guild.id)
            ratings = [(rating(user), 1) for user_id, user in res if user.rating is not None]
            user_str = '+server'
        else:
            def normalize(x):
                return [i.lower() for i in x]
            handle_counts = {}
            parsed_handles = []
            for i in handles:
                parse_str = normalize(i.split('*'))
                if len(parse_str) > 1:
                    try:
                        handle_counts[parse_str[0]] = int(parse_str[1])
                    except ValueError:
                        raise CodeforcesCogError("Can't multiply by non-integer")
                else:
                    handle_counts[parse_str[0]] = 1
                parsed_handles.append(parse_str[0])

            cf_handles = await cf_common.resolve_handles(ctx, self.converter, parsed_handles, mincnt=1, maxcnt=1000)
            cf_handles = normalize(cf_handles)
            cf_to_original = {a: b for a, b in zip(cf_handles, parsed_handles)}
            original_to_cf = {a: b for a, b in zip(parsed_handles, cf_handles)}
            users = await cf.user.info(handles=cf_handles)
            user_strs = []
            for a, b in handle_counts.items():
                if b > 1:
                    user_strs.append(f'{original_to_cf[a]}*{b}')
                elif b == 1:
                    user_strs.append(original_to_cf[a])
                elif b <= 0:
                    raise CodeforcesCogError('How can you have nonpositive members in team?')

            user_str = ', '.join(user_strs)
            ratings = [(rating(user), handle_counts[cf_to_original[user.handle.lower()]])
                       for user in users if user.rating]

        if len(ratings) == 0:
            raise CodeforcesCogError("No CF usernames with ratings passed in.")

        left = -100.0
        right = 10000.0
        teamRating = Codeforces.composeRatings(left, right, ratings)
        embed = discord.Embed(title=user_str, description=teamRating, color=cf.rating2rank(teamRating).color_embed)
        await ctx.send(embed = embed)

    @discord_common.send_error_if(CodeforcesCogError, cf_common.ResolveHandleError,
                                  cf_common.FilterError)
    async def cog_command_error(self, ctx, error):
        pass


async def setup(bot):
    await bot.add_cog(Codeforces(bot))
