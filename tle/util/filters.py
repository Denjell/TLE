"""Labelled slash command options for the Codeforces problem and submission filters.

Several commands grew up around a single free-text field of prefix flags:
`+dp ~greedy r>=1500 d>=012024 +contest`. That reads well enough typed after
`;plot solved`, but a slash command gives every option a labelled box of its
own, and one box called `args` throws that away. The pieces are taken apart
here so the commands can share them; the string form still lives in
codeforces_common, for the prefix parsers that read it back out of a message.

Everything raised here is a ParamParseError, so any cog that already routes
cf_common.FilterError to an alert embed can use these unchanged.
"""
import datetime
import shlex
import time

from discord import app_commands

# codeforces_common has to be imported before cache_system2: the two form a
# cycle through codeforces_api, and only this order enters it from a side that
# completes.
from tle.util import codeforces_common as cf_common
from tle.util import cache_system2


# Codeforces participant types, under the names the slash options offer.
SUBMISSION_TYPES = {
    'contest': 'CONTESTANT',
    'out_of_competition': 'OUT_OF_COMPETITION',
    'virtual': 'VIRTUAL',
    'practice': 'PRACTICE',
}

# Date options are read as bare digits, by length. Both orders are offered:
# day-first is what the 'd>=' prefix syntax has always taken, year-first is what
# a labelled field invites. They cannot collide for a real date, since 01032024
# is no year 0103 and 20240301 is no day 20 of a month 24.
_DATE_SEPARATORS = str.maketrans('', '', '-/. ')
_DATE_FORMATS = {4: ('%Y',), 6: ('%m%Y', '%Y%m'), 8: ('%d%m%Y', '%Y%m%d')}

# Problem ratings run 800-3500; SubFilter's submission bounds are wider because
# they also have to cover the unrated end of the scale.
ProblemRating = app_commands.Range[int, 800, 3500]
SubmissionRating = app_commands.Range[int, 500, 3800]

_DESCRIPTIONS = {
    'handle': 'A Codeforces handle or Discord mention. Defaults to you.',
    'handles': 'Codeforces handles or Discord mentions, space separated; quote names with spaces. Defaults to you.',
    'tags': 'Only problems with these tags, comma separated.',
    'exclude_tags': 'Skip problems with these tags, comma separated.',
    'division': 'Only problems from these divisions, comma separated.',
    'exclude_division': 'Skip problems from these divisions, comma separated.',
    'min_rating': 'Only problems rated at least this.',
    'max_rating': 'Only problems rated at most this.',
    'after': 'Only submissions from this date on, as 2024, 2024-03 or 2024-03-01.',
    'before': 'Only submissions before this date, as 2024, 2024-03 or 2024-03-01.',
    'types': 'Submission types, comma separated. All of them by default.',
    'contests': 'Only these contests, comma separated, matched against the contest name.',
    'indices': 'Only these problem indices, comma separated, such as A or C1.',
    'include_team': 'Include problems solved as part of a team.',
}

# The options build_sub_filter takes, in the order a command should declare
# them. Spelled out so a command can hand the same list to describe().
SUB_FILTER_OPTIONS = ('tags', 'exclude_tags', 'division', 'exclude_division',
                      'min_rating', 'max_rating', 'after', 'before', 'types',
                      'contests', 'indices', 'include_team')


def describe(*names, **extra):
    """app_commands.describe for the standard filter options, chosen by name.

    Saves repeating the same fourteen strings in every command that offers the
    same fourteen options, and keeps them worded identically where it matters
    that they are in fact the same option.
    """
    texts = {name: _DESCRIPTIONS[name] for name in names}
    texts.update(extra)
    return app_commands.describe(**texts)


def complete_list_field(current, known):
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


async def tag_autocomplete(interaction, current: str):
    """Suggest Codeforces tags.

    There are more tags than the 25 choice limit allows, which is why this is
    autocomplete rather than a dropdown.
    """
    known = sorted({tag for problem in cf_common.cache2.problem_cache.problems
                    for tag in problem.tags
                    if tag not in cache_system2._DIV_TAGS})
    return complete_list_field(current, known)


async def submission_type_autocomplete(interaction, current: str):
    """Suggest submission types.

    These would fit in a dropdown, but a dropdown holds one value and asking
    for, say, contest and virtual together is a fair thing to want.
    """
    return complete_list_field(current, sorted(SUBMISSION_TYPES))


async def division_autocomplete(interaction, current: str):
    """Suggest divisions for a comma separated division/exclude_division field.

    There are only five of these, so each alone would fit a dropdown - the
    comma separation (and this autocomplete, to spell them correctly) is what
    lets more than one be picked at once, the same way the tags field does.
    """
    return complete_list_field(current, cache_system2._DIV_TAGS)


def split_list(text):
    """Split a comma separated field into its entries."""
    return [entry.strip() for entry in text.split(',') if entry.strip()]


def split_words(text):
    """Split a space separated field, keeping a "double quoted" run together.

    The prefix parser used to do this for free, and it is what lets a field
    hold `"!name with spaces"` or a country such as `"United States"`. Only
    double quotes group, so an apostrophe in a name is just a character, and
    neither '#' (as in a `name#0` username) nor a backslash means anything.
    """
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.quotes = '"'
    lexer.escape = ''
    lexer.commenters = ''
    try:
        return list(lexer)
    except ValueError:
        # An unclosed quote. Reading the text as plain words is what the field
        # did before quoting was understood, and it beats refusing outright.
        return text.split()


def split_tags(text, field, *, tags_cost_points=False):
    """Split a comma separated tag field.

    Comma rather than space because 14 of the Codeforces tags contain one
    ('binary search', 'data structures'), which the old '+tag' syntax could not
    express at all.

    Division tags are rejected rather than accepted quietly. Every command
    offering this field offers a division option too, which filters the same
    way without the gitgud tag penalty.
    """
    tags = split_list(text)
    divisions = [tag for tag in tags if tag in cache_system2._DIV_TAGS]
    if divisions:
        message = f'`{", ".join(divisions)}` belongs in the division option, not `{field}`.'
        if tags_cost_points:
            message += ' Filtering by division there costs no points, while a tag costs 200.'
        raise cf_common.ParamParseError(message)
    return tags


def split_divisions(text, field):
    """Split a comma separated division field, rejecting anything that is not
    one of the five division tags.
    """
    divisions = split_list(text)
    unknown = [division for division in divisions if division not in cache_system2._DIV_TAGS]
    if unknown:
        raise cf_common.ParamParseError(
            f'`{", ".join(unknown)}` is not a division for `{field}`. '
            f'Pick from `{", ".join(cache_system2._DIV_TAGS)}`.')
    return divisions


def problem_tags(tags, exclude_tags, division, exclude_division, *,
                 tags_cost_points=False):
    """Split the tag and division fields and fold the divisions into tags/bantags.

    Divisions are matched exactly like real tags - the same
    Problem.matches_all_tags AND that `tags` has always used - rather than
    "matches any of these given". That mirrors the behaviour this field had
    before it took labelled options at all: the old `+div2`/`~div2` prefix
    syntax never distinguished a division from any other tag, so asking for
    two was always an AND, same as asking for two real tags. In practice that
    means `division: div2, div3` only matches a problem that is both at once
    (a combined Div. 1 + Div. 2 round, say) - intentional, not a bug.

    Returns (tags, bantags, tagged). `tagged` reports whether a real tag was
    asked for, which is what gitgud charges 200 points for; divisions never
    count towards it (same as on master), which is why it is read before
    either division option is folded in.
    """
    tags = split_tags(tags, 'tags', tags_cost_points=tags_cost_points)
    bantags = split_tags(exclude_tags, 'exclude_tags', tags_cost_points=tags_cost_points)
    tagged = bool(tags or bantags)
    tags += split_divisions(division, 'division')
    bantags += split_divisions(exclude_division, 'exclude_division')
    return tags, bantags, tagged


def split_types(text):
    """Split a comma separated submission type field into Codeforces values."""
    names = [name.lower() for name in split_list(text)]
    unknown = [name for name in names if name not in SUBMISSION_TYPES]
    if unknown:
        raise cf_common.ParamParseError(
            f'`{", ".join(unknown)}` is not a submission type. '
            f'Pick from `{", ".join(sorted(SUBMISSION_TYPES))}`.')
    # An empty field means every type, which is what SubFilter.parse does.
    return [SUBMISSION_TYPES[name] for name in names] or list(SUBMISSION_TYPES.values())


def parse_date(text, field):
    """Parse a date option into a timestamp.

    cf_common.parse_date takes a bare ddmmyyyy, mmyyyy or yyyy, which reads
    fine behind a 'd>=' prefix but looks like a typo in a labelled field, so
    separators are dropped and year-first order is accepted alongside it.
    """
    digits = text.translate(_DATE_SEPARATORS)
    for fmt in _DATE_FORMATS.get(len(digits), ()):
        try:
            return time.mktime(datetime.datetime.strptime(digits, fmt).timetuple())
        except ValueError:
            continue
    raise cf_common.ParamParseError(
        f'`{text}` is not a valid date for `{field}`. Give a year (2024), '
        'a month (2024-03) or a day (2024-03-01).')


def parse_month(text, field):
    """Parse a month option into the timestamp of its first day.

    parse_date also takes a bare year or a full date, which a month field would
    then quietly read as January or as the month around that day, so only the
    month forms are let through.
    """
    if len(text.translate(_DATE_SEPARATORS)) == 6:
        try:
            return parse_date(text, field)
        except cf_common.ParamParseError:
            pass
    raise cf_common.ParamParseError(
        f'`{text}` is not a valid month for `{field}`. Give it as 2024-03.')


def date_range(after, before):
    """Turn the after/before options into the (dlo, dhi) pair used everywhere."""
    dlo = 0 if after is None else parse_date(after, 'after')
    dhi = 10**10 if before is None else parse_date(before, 'before')
    return dlo, dhi


def build_sub_filter(*, rated=True, tags='', exclude_tags='', division='',
                     exclude_division='', min_rating=None, max_rating=None,
                     after=None, before=None, types='', contests='',
                     indices='', include_team=False):
    """Build a SubFilter from the labelled options.

    Assignment rather than SubFilter.parse, which exists to read the old string
    syntax back out of a message.
    """
    filt = cf_common.SubFilter(rated)
    filt.tags, filt.bantags, _ = problem_tags(tags, exclude_tags, division,
                                              exclude_division)
    if min_rating is not None:
        filt.rlo = min_rating
    if max_rating is not None:
        filt.rhi = max_rating
    if min_rating is not None or max_rating is not None:
        # An unrated problem satisfies no rating bound, so asking for one turns
        # the flag on. It is never turned off here: a caller that started the
        # filter rated meant it.
        filt.rated = True
    filt.dlo, filt.dhi = date_range(after, before)
    filt.types = split_types(types)
    filt.contests = split_list(contests)
    filt.indices = split_list(indices)
    filt.team = include_team
    return filt
