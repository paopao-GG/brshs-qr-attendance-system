"""The records password.

One shared password guards the attendance records screen. Three things about that are
worth stating plainly, because the paper will make claims about record integrity:

1. **It is stored as an argon2 hash in the database**, never in `config.toml`, which is
   committed to git, and never in plain text anywhere.

2. **There is no default.** First use asks the operator to *set* a password. A known
   default shipped in the client's repository is a back door, not a convenience, and
   "we meant to change it" is how every one of those ends.

3. **It authenticates nobody.** It proves someone knew a password, which is why every
   correction separately asks for a name and why that name is stored apart from
   `corrected_by`. See core/corrections.py.

Qt-free, like the rest of core/.
"""

from __future__ import annotations

import sqlite3
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .db import utcnow

PASSWORD_KEY = "records_password_hash"
MIN_LENGTH = 4

# Deters a student who finds an unattended keyboard. It does NOT stop someone who can
# restart the application -- the counter lives in this process only. Persisting it
# would let a locked-out kiosk stay locked out across a reboot, which at a school gate
# is the worse failure of the two.
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 60

_hasher = PasswordHasher()


class PasswordError(RuntimeError):
    """Wrong password, a weak new one, or too many attempts."""


def _get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _put(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                          updated_at = excluded.updated_at""",
        (key, value, utcnow()),
    )


def is_set(conn: sqlite3.Connection) -> bool:
    """False on a fresh database, which is what puts the UI into 'set one' mode."""
    return _get(conn, PASSWORD_KEY) is not None


def set_password(
    conn: sqlite3.Connection, new: str, *, current: str | None = None,
    actor_name: str | None = None,
) -> None:
    """Set the password, or change it.

    Changing requires the current one. Without that check, anyone who walked up to an
    already-unlocked screen could lock the real staff out of their own records.
    """
    from .db import audit          # local import: db imports nothing from here

    if is_set(conn):
        if current is None:
            raise PasswordError("changing the password requires the current one")
        verify(conn, current)

    if len(new or "") < MIN_LENGTH:
        raise PasswordError(f"the password must be at least {MIN_LENGTH} characters")

    existed = is_set(conn)
    _put(conn, PASSWORD_KEY, _hasher.hash(new))

    # The password itself is never in the audit row -- only that it changed.
    audit(
        conn, "records_password.changed" if existed else "records_password.set",
        entity_type="app_settings", entity_id=PASSWORD_KEY, actor_name=actor_name,
    )


def verify(conn: sqlite3.Connection, attempt: str) -> None:
    """Raise PasswordError unless `attempt` is right. Rehashes if parameters moved on."""
    stored = _get(conn, PASSWORD_KEY)
    if stored is None:
        raise PasswordError("no records password has been set yet")

    try:
        _hasher.verify(stored, attempt or "")
    except (VerifyMismatchError, InvalidHashError):
        raise PasswordError("that password is not correct") from None

    if _hasher.check_needs_rehash(stored):
        _put(conn, PASSWORD_KEY, _hasher.hash(attempt))


class AttemptGate:
    """Counts wrong attempts and locks out for a while.

    A clock is injected so the lockout can be tested without sleeping through it.
    """

    def __init__(self, *, max_attempts: int = MAX_ATTEMPTS,
                 lockout_seconds: int = LOCKOUT_SECONDS, clock=time.monotonic) -> None:
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self._clock = clock
        self._failures = 0
        self._locked_until = 0.0

    @property
    def seconds_remaining(self) -> int:
        return max(0, int(self._locked_until - self._clock() + 0.999))

    @property
    def is_locked(self) -> bool:
        if self._clock() >= self._locked_until:
            # The window expired. Reset the count too, or the very next wrong attempt
            # would re-lock immediately and the lockout would never really end.
            if self._locked_until:
                self._locked_until = 0.0
                self._failures = 0
            return False
        return True

    def check(self, conn: sqlite3.Connection, attempt: str) -> None:
        """Verify, counting failures. Raises PasswordError either way when wrong."""
        if self.is_locked:
            raise PasswordError(
                f"too many attempts - locked for {self.seconds_remaining}s"
            )
        try:
            verify(conn, attempt)
        except PasswordError:
            self._failures += 1
            if self._failures >= self.max_attempts:
                self._locked_until = self._clock() + self.lockout_seconds
                raise PasswordError(
                    f"too many attempts - locked for {self.lockout_seconds}s"
                ) from None
            left = self.max_attempts - self._failures
            raise PasswordError(
                f"that password is not correct - {left} attempt(s) left"
            ) from None

        self._failures = 0
