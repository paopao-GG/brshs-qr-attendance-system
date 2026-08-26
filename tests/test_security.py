"""The records password."""
import pytest

from trackify.core import security
from trackify.core.security import AttemptGate, PasswordError


def test_a_fresh_database_has_no_password(conn):
    """Which is what puts the dialog into 'set one' mode rather than 'guess one'."""
    assert not security.is_set(conn)


def test_no_default_password_is_shipped(conn):
    """A known default in the client's repository is a back door, not a convenience."""
    for guess in ("", "admin", "password", "1234", "trackify", "TRACKIFY"):
        with pytest.raises(PasswordError, match="no records password"):
            security.verify(conn, guess)


def test_setting_then_verifying(conn):
    security.set_password(conn, "gate-2026")
    assert security.is_set(conn)
    security.verify(conn, "gate-2026")


def test_the_wrong_password_is_refused(conn):
    security.set_password(conn, "gate-2026")
    with pytest.raises(PasswordError, match="not correct"):
        security.verify(conn, "gate-2025")


def test_the_password_is_never_stored_in_plain_text(conn):
    security.set_password(conn, "gate-2026")
    stored = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (security.PASSWORD_KEY,)
    ).fetchone()["value"]

    assert "gate-2026" not in stored
    assert stored.startswith("$argon2")


def test_changing_requires_the_current_password(conn):
    """Otherwise anyone at an already-unlocked screen could lock the staff out of
    their own records."""
    security.set_password(conn, "gate-2026")

    with pytest.raises(PasswordError, match="requires the current one"):
        security.set_password(conn, "new-one")

    with pytest.raises(PasswordError, match="not correct"):
        security.set_password(conn, "new-one", current="wrong")

    security.set_password(conn, "new-one", current="gate-2026")
    security.verify(conn, "new-one")


def test_a_too_short_password_is_refused(conn):
    with pytest.raises(PasswordError, match="at least"):
        security.set_password(conn, "ab")


def test_setting_and_changing_are_audited_without_the_password(conn):
    security.set_password(conn, "gate-2026", actor_name="School Head")
    security.set_password(conn, "second-one", current="gate-2026",
                          actor_name="School Head")

    rows = conn.execute(
        "SELECT action, actor_name FROM audit_log ORDER BY id"
    ).fetchall()
    assert [r["action"] for r in rows] == [
        "records_password.set", "records_password.changed",
    ]
    assert rows[0]["actor_name"] == "School Head"

    dump = str([tuple(r) for r in conn.execute("SELECT * FROM audit_log")])
    assert "gate-2026" not in dump
    assert "second-one" not in dump


# --- the attempt gate -------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_wrong_attempts_count_down_out_loud(conn):
    security.set_password(conn, "gate-2026")
    gate = AttemptGate(max_attempts=3)

    with pytest.raises(PasswordError, match="2 attempt"):
        gate.check(conn, "no")
    with pytest.raises(PasswordError, match="1 attempt"):
        gate.check(conn, "no")


def test_too_many_attempts_locks_out(conn):
    security.set_password(conn, "gate-2026")
    clock = FakeClock()
    gate = AttemptGate(max_attempts=3, lockout_seconds=60, clock=clock)

    for _ in range(3):
        with pytest.raises(PasswordError):
            gate.check(conn, "no")

    assert gate.is_locked
    # Even the RIGHT password is refused while locked, or the lockout is decorative.
    with pytest.raises(PasswordError, match="too many attempts"):
        gate.check(conn, "gate-2026")


def test_the_lockout_expires_and_fully_resets(conn):
    """If the counter survived the lockout, the next wrong attempt would re-lock
    instantly and the lockout would never really end."""
    security.set_password(conn, "gate-2026")
    clock = FakeClock()
    gate = AttemptGate(max_attempts=3, lockout_seconds=60, clock=clock)

    for _ in range(3):
        with pytest.raises(PasswordError):
            gate.check(conn, "no")

    clock.now += 61
    assert not gate.is_locked
    gate.check(conn, "gate-2026")            # works again

    with pytest.raises(PasswordError, match="2 attempt"):
        gate.check(conn, "no")               # counting from scratch


def test_a_correct_password_clears_the_count(conn):
    security.set_password(conn, "gate-2026")
    gate = AttemptGate(max_attempts=3)

    with pytest.raises(PasswordError):
        gate.check(conn, "no")
    gate.check(conn, "gate-2026")

    with pytest.raises(PasswordError, match="2 attempt"):
        gate.check(conn, "no")


def test_seconds_remaining_is_reported_for_the_screen(conn):
    security.set_password(conn, "gate-2026")
    clock = FakeClock()
    gate = AttemptGate(max_attempts=1, lockout_seconds=60, clock=clock)

    with pytest.raises(PasswordError):
        gate.check(conn, "no")

    assert gate.seconds_remaining == 60
    clock.now += 30
    assert gate.seconds_remaining == 30
