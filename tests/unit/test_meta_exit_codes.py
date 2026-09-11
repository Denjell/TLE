"""Tests for the exit codes `;meta restart` and `;meta kill` rely on.

This feature is split across two files: the cog picks an exit code, and the
compose file's restart policy decides what that code means. Neither half is
useful alone, so both are pinned here.
"""

import re
from pathlib import Path

import pytest

from tle.cogs.meta import _RESTART_EXIT_CODE, _SHUTDOWN_EXIT_CODE

pytestmark = pytest.mark.unit

_COMPOSE_FILE = Path(__file__).resolve().parents[2] / 'docker-compose.yaml'

# Policies that bring a container back after a non-zero exit. `no` and
# `on-success` do not, and would leave `;meta restart` acting as a shutdown.
_RESTARTING_POLICIES = ('on-failure', 'always', 'unless-stopped')


class TestExitCodes:
    def test_restart_is_non_zero(self):
        # A zero exit reads as a clean shutdown, which no restart policy
        # short of `always` acts on.
        assert _RESTART_EXIT_CODE != 0

    def test_shutdown_is_zero(self):
        assert _SHUTDOWN_EXIT_CODE == 0

    def test_codes_differ(self):
        assert _RESTART_EXIT_CODE != _SHUTDOWN_EXIT_CODE

    def test_restart_code_is_a_valid_exit_status(self):
        # Anything outside 0-255 is truncated by the OS, which could collide
        # with the shutdown code.
        assert 0 < _RESTART_EXIT_CODE < 256


class TestComposeRestartPolicy:
    @staticmethod
    def _policy():
        text = _COMPOSE_FILE.read_text(encoding='utf-8')
        matches = re.findall(r'^\s*restart:\s*["\']?([\w:-]+)', text, re.MULTILINE)
        assert matches, f'No restart policy found in {_COMPOSE_FILE.name}'
        assert len(matches) == 1, f'Expected one restart policy, got {matches}'
        return matches[0]

    def test_compose_file_exists(self):
        assert _COMPOSE_FILE.is_file()

    def test_policy_restarts_on_non_zero_exit(self):
        policy = self._policy()
        assert policy.split(':')[0] in _RESTARTING_POLICIES, (
            f'restart: {policy} does not bring the bot back after a non-zero'
            ' exit, so `;meta restart` would behave as a shutdown.'
        )

    def test_policy_does_not_restart_after_clean_exit(self):
        # `always` and `unless-stopped` ignore the exit code, which would
        # make `;meta kill` unable to keep the bot down.
        policy = self._policy().split(':')[0]
        assert policy == 'on-failure', (
            f'restart: {policy} restarts regardless of exit code, so'
            ' `;meta kill` could not keep the bot down.'
        )
