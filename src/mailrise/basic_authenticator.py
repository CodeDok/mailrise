"""
This is the authentication functionality for the SMTP server.
"""

from __future__ import annotations

import hmac
import time
import typing as typ
from collections import deque
from logging import Logger

from aiosmtpd import smtp


# Compared against when the username is unknown, so that a failed login takes
# the same amount of time whether or not the username exists.
_DUMMY_PASSWORD = b'mailrise-dummy-password-for-constant-time-comparison'

# Sent when a client has exceeded the failed login limit.
_LOCKED_OUT = '454 4.7.0 Too many failed authentication attempts, try again later'

# Prune the failure table once it tracks this many peers.
_PRUNE_THRESHOLD = 1024


class BasicAuthenticator:
    """A simple authenticator that uses a static username and password list.

    Failed logins are tracked per client IP address. Once a client exceeds
    `max_failures` failed logins within `lockout_seconds`, all of its login
    attempts are rejected until the window expires.

    Attributes:
        logins: A mapping of usernames to passwords.
        max_failures: The number of failed logins allowed per client within
            the lockout window. Zero disables the lockout.
        lockout_seconds: The length of the lockout window.
    """
    logins: typ.Mapping[str, str]
    max_failures: int
    lockout_seconds: float

    def __init__(self, logins: typ.Mapping[str, str], max_failures: int = 5,
                 lockout_seconds: float = 300, logger: Logger | None = None) -> None:
        self.logins = logins
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self._logger = logger
        self._failures: dict[str, deque[float]] = {}

    # pylint: disable=too-many-arguments
    def __call__(self, server: smtp.SMTP, session: smtp.Session,
                 envelope: smtp.Envelope, mechanism: str, auth_data: typ.Any) \
            -> smtp.AuthResult:
        fail_nothandled = smtp.AuthResult(success=False, handled=False)
        if mechanism not in ("LOGIN", "PLAIN"):
            return fail_nothandled
        if not isinstance(auth_data, smtp.LoginPassword):
            return fail_nothandled

        peer = _peer_host(session)
        now = time.monotonic()
        if self._is_locked_out(peer, now):
            self._log('Rejected login from locked out client %s', peer)
            return smtp.AuthResult(success=False, handled=False, message=_LOCKED_OUT)

        username = auth_data.login.decode('utf-8', errors='replace')
        expected = self.logins.get(username)
        expected_bytes = expected.encode('utf-8') if expected is not None else _DUMMY_PASSWORD
        password_ok = hmac.compare_digest(auth_data.password, expected_bytes)

        if expected is not None and password_ok:
            self._failures.pop(peer, None)
            return smtp.AuthResult(success=True, auth_data=username)

        self._record_failure(peer, now)
        self._log('Failed login from %s for user %r', peer, username)
        # handled=False makes aiosmtpd reply with "535 Authentication
        # credentials invalid". With handled=True, it would send nothing at all.
        return fail_nothandled

    def _is_locked_out(self, peer: str, now: float) -> bool:
        if self.max_failures <= 0:
            return False
        failures = self._failures.get(peer)
        if not failures:
            return False
        _expire(failures, now - self.lockout_seconds)
        return len(failures) >= self.max_failures

    def _record_failure(self, peer: str, now: float) -> None:
        if self.max_failures <= 0:
            return
        if len(self._failures) >= _PRUNE_THRESHOLD:
            self._prune(now)
        failures = self._failures.setdefault(peer, deque())
        failures.append(now)
        _expire(failures, now - self.lockout_seconds)

    def _prune(self, now: float) -> None:
        cutoff = now - self.lockout_seconds
        for peer in list(self._failures):
            failures = self._failures[peer]
            _expire(failures, cutoff)
            if not failures:
                del self._failures[peer]

    def _log(self, msg: str, *args: typ.Any) -> None:
        if self._logger is not None:
            self._logger.warning(msg, *args)

    def __str__(self) -> str:
        return f'Basic({len(self.logins)})'


def _expire(failures: deque[float], cutoff: float) -> None:
    while failures and failures[0] <= cutoff:
        failures.popleft()


def _peer_host(session: smtp.Session) -> str:
    """Extract the client's host address, ignoring the source port."""
    peer = getattr(session, 'peer', None)
    if isinstance(peer, tuple) and peer:
        return str(peer[0])
    return str(peer)
