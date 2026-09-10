"""Component tests for how tle.util.codeforces_api shapes its HTTP requests.

The aiohttp session is replaced with a recorder, so these assert the request
that would go out without touching the network.
"""

from contextlib import ExitStack, asynccontextmanager
from unittest.mock import patch

import pytest

from tle.util import codeforces_api as cf

pytestmark = pytest.mark.component


@asynccontextmanager
async def _recording_session(payload):
    """Swaps in a recording session and drops the 1 request/second limiter,
    which would otherwise pace these tests at a second apiece.
    """
    rec = _Recorder(payload)
    with ExitStack() as stack:
        stack.enter_context(patch.object(cf, '_session', rec))
        stack.enter_context(
            patch.object(cf, '_query_api', cf._query_api.__wrapped__)
        )
        yield rec


class _Recorder:
    """Stands in for aiohttp.ClientSession, capturing calls."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def _record(self, method):
        @asynccontextmanager
        async def call(url, *, params=None, data=None, headers=None):
            self.calls.append(
                {'method': method, 'url': url, 'params': params, 'data': data}
            )
            yield _Response(self.payload)

        return call

    def get(self, url, **kwargs):
        return self._record('get')(url, **kwargs)

    def post(self, url, **kwargs):
        return self._record('post')(url, **kwargs)


class _Response:
    status = 200

    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return {'status': 'OK', 'result': self._payload}


_EMPTY_STANDINGS = {
    'contest': {
        'id': 1700,
        'name': 'Test Round',
        'type': 'CF',
        'phase': 'FINISHED',
        'frozen': False,
        'durationSeconds': 7200,
        'startTimeSeconds': 1,
        'relativeTimeSeconds': 2,
        'preparedBy': None,
    },
    'problems': [],
    'rows': [],
}


@pytest.fixture
async def recorder():
    async with _recording_session(_EMPTY_STANDINGS) as rec:
        yield rec


class TestContestStandingsRequest:
    """Codeforces answers non-gym contest.standings for unauthenticated
    clients only as a GET carrying contestId alone. A POST, or any extra
    parameter, returns HTTP 400 and takes down the problemset and ranklist
    caches with it.
    """

    async def test_uses_get(self, recorder):
        await cf.contest.standings(contest_id=1700)
        assert recorder.calls[0]['method'] == 'get'

    async def test_sends_only_contest_id(self, recorder):
        await cf.contest.standings(contest_id=1700)
        assert recorder.calls[0]['params'] == {'contestId': 1700}

    @pytest.mark.parametrize(
        'kwargs',
        [
            {'from_': 1, 'count': 1},
            {'show_unofficial': True},
            {'show_unofficial': False},
            {'handles': ['tourist']},
            {'room': 1},
        ],
        ids=['from_count', 'unofficial', 'official', 'handles', 'room'],
    )
    async def test_extra_parameters_are_not_sent(self, recorder, kwargs):
        await cf.contest.standings(contest_id=1700, **kwargs)
        assert recorder.calls[0]['params'] == {'contestId': 1700}
        assert recorder.calls[0]['data'] is None

    async def test_row_changing_parameters_warn(self, recorder, caplog):
        await cf.contest.standings(contest_id=1700, show_unofficial=True)
        assert 'authenticated' in caplog.text

    async def test_trimming_parameters_do_not_warn(self, recorder, caplog):
        await cf.contest.standings(contest_id=1700, from_=1, count=1)
        assert not caplog.text


class TestOtherEndpointsUsePost:
    """POST lifts the URL length limit that would otherwise cap user.info's
    handle list, and Codeforces accepts it everywhere except contest.standings.
    """

    async def test_user_info_uses_post(self):
        async with _recording_session([]) as rec:
            await cf.user.info(handles=['tourist'])
        assert rec.calls[0]['method'] == 'post'

    async def test_contest_to_list_uses_post(self):
        async with _recording_session([]) as rec:
            await cf.contest.to_list()
        assert rec.calls[0]['method'] == 'post'
