from aiocache import cached
from discord.ext import commands

from tle.util import codeforces_api as cf

CONTEST_BLACKLIST = {1308, 1309, 1431, 1432}
_CONTESTS_PER_BATCH_IN_CACHE_UPDATES = 100

# Division tags synthesized onto problems based on the contest they came from,
# e.g. so `;gitgud +div2` can filter on them like any other problem tag.
DIV_TAGS = ['div1', 'div2', 'div3', 'div4', 'edu']


def _is_blacklisted(contest: cf.Contest) -> bool:
    return contest.id in CONTEST_BLACKLIST


class CacheError(commands.CommandError):
    pass


@cached(ttl=30 * 60)
async def getUsersEffectiveRating(*, activeOnly: bool | None = None) -> dict[str, int]:
    """Returns a mapping from user handles to their effective rating."""
    ratedList = await cf.user.ratedList(activeOnly=activeOnly)
    return {user.handle: user.effective_rating for user in ratedList}
