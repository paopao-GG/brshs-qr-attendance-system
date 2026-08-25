"""Direction state machine, debounce, and derived status.

Config defaults in play: entry_open 06:00, late_threshold 07:30,
early_departure_cutoff 15:30, dismissal 16:00, debounce 5 min, max 6 scans/day.
"""

from trackify.core.attendance import Outcome, Trigger, close_open_days, record_scan
from trackify.core.sessions import suspend_day

from .conftest import at


# --- direction state machine ------------------------------------------------

def test_first_scan_is_in_second_is_out(conn, student, config):
    first = record_scan(conn, student, at(7, 0), config)
    assert first.outcome is Outcome.RECORDED_IN
    assert first.direction == "in"

    second = record_scan(conn, student, at(16, 5), config)
    assert second.outcome is Outcome.RECORDED_OUT
    assert second.direction == "out"


def test_third_scan_requires_override(conn, student, config):
    record_scan(conn, student, at(7, 0), config)
    record_scan(conn, student, at(16, 5), config)
    third = record_scan(conn, student, at(16, 30), config)
    assert third.outcome is Outcome.NEEDS_OVERRIDE
    assert not third.recorded


def test_override_permits_reentry(conn, student, config):
    record_scan(conn, student, at(7, 0), config)
    record_scan(conn, student, at(11, 0), config)
    third = record_scan(conn, student, at(13, 0), config,
                        override_reason="returned from medical appointment")
    assert third.outcome is Outcome.RECORDED_IN
    reason = conn.execute(
        "SELECT override_reason FROM scan_events ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert "medical" in reason


# --- debounce ---------------------------------------------------------------

def test_rapid_rescan_produces_exactly_one_row(conn, student, config):
    """Step 6 pass condition."""
    record_scan(conn, student, at(7, 0), config)
    for extra in (1, 2, 3):
        result = record_scan(conn, student, at(7, extra), config)
        assert result.outcome is Outcome.DEBOUNCED

    count = conn.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0]
    assert count == 1


def test_debounce_spans_directions_not_just_the_same_one(conn, student, config):
    """The dangerous case: student scans in, re-taps 30s later unsure it worked.

    A per-direction debounce would record a DEPARTURE and text the guardian
    'left school' at 7am. Debounce must span directions.
    """
    record_scan(conn, student, at(7, 0), config)
    second = record_scan(conn, student, at(7, 0).replace(second=30), config)

    assert second.outcome is Outcome.DEBOUNCED
    assert second.direction == "in"          # reports the existing state
    assert Trigger.DEPARTURE not in second.triggers
    assert conn.execute(
        "SELECT COUNT(*) FROM scan_events WHERE direction = 'out'"
    ).fetchone()[0] == 0


def test_scan_after_debounce_window_is_accepted(conn, student, config):
    record_scan(conn, student, at(7, 0), config)
    later = record_scan(conn, student, at(7, 6), config)
    assert later.outcome is Outcome.RECORDED_OUT


def test_debounce_message_names_the_earlier_time(conn, student, config):
    record_scan(conn, student, at(7, 5), config)
    result = record_scan(conn, student, at(7, 7), config)
    assert "7:05 AM" in result.message


# --- status and flags -------------------------------------------------------

def test_on_time_arrival(conn, student, config):
    result = record_scan(conn, student, at(7, 0), config)
    assert result.status == "present"
    assert Trigger.ARRIVAL in result.triggers


def test_late_arrival(conn, student, config):
    result = record_scan(conn, student, at(7, 45), config)
    assert result.status == "late"
    assert Trigger.LATE in result.triggers
    assert Trigger.ARRIVAL not in result.triggers


def test_late_counts_as_present_for_attendance(conn, student, config):
    """analytics-model.md section 1: tardiness is modelled separately, not folded in."""
    record_scan(conn, student, at(7, 45), config)
    status = conn.execute("SELECT status FROM attendance_days").fetchone()[0]
    assert status == "late"


def test_early_departure_flagged(conn, student, config):
    record_scan(conn, student, at(7, 0), config)
    result = record_scan(conn, student, at(12, 0), config)
    assert "early_departure" in result.flags


def test_normal_departure_not_flagged_early(conn, student, config):
    record_scan(conn, student, at(7, 0), config)
    result = record_scan(conn, student, at(16, 10), config)
    assert "early_departure" not in result.flags


def test_scan_before_gate_opens_is_flagged(conn, student, config):
    result = record_scan(conn, student, at(5, 30), config)
    assert "out_of_window" in result.flags
    assert result.recorded  # recorded anyway, flagged for adviser review


def test_minutes_on_campus_computed(conn, student, config):
    record_scan(conn, student, at(7, 0), config)
    record_scan(conn, student, at(16, 0), config)
    minutes = conn.execute("SELECT minutes_on_campus FROM attendance_days").fetchone()[0]
    assert minutes == 540


def test_manual_entry_flagged(conn, student, config):
    result = record_scan(conn, student, at(7, 0), config, method="manual")
    assert "manual_entry" in result.flags


# --- guards -----------------------------------------------------------------

def test_suspended_day_rejects_scans(conn, student, config):
    suspend_day(conn, "2026-09-01", "Typhoon signal no. 2", config)
    result = record_scan(conn, student, at(7, 0), config)
    assert result.outcome is Outcome.NOT_A_SCHOOL_DAY
    assert "Typhoon" in result.message


def test_scan_cap_enforced(conn, student, config):
    minute = 0
    for _ in range(config.scanning.max_scans_per_day):
        record_scan(conn, student, at(7, minute), config,
                    override_reason="test setup")
        minute += 10
    result = record_scan(conn, student, at(7, minute), config)
    assert result.outcome is Outcome.SCAN_CAP_REACHED


def test_scan_events_are_never_updated(conn, student, config):
    """Raw scans immutable; corrections live in attendance_days."""
    record_scan(conn, student, at(7, 0), config)
    record_scan(conn, student, at(16, 0), config)
    rows = conn.execute(
        "SELECT direction FROM scan_events ORDER BY id"
    ).fetchall()
    assert [r[0] for r in rows] == ["in", "out"]


# --- end of day -------------------------------------------------------------

def test_no_scan_at_all_marks_absent(conn, make_student, config):
    make_student()
    make_student()
    result = close_open_days(conn, "2026-09-01", config)
    assert result.absent == 2
    assert result.exit_missing == 0
    statuses = [r[0] for r in conn.execute("SELECT status FROM attendance_days")]
    assert statuses == ["absent", "absent"]


def test_missing_exit_scan_is_flagged_not_texted(conn, student, config):
    """Texting a parent 'no departure recorded' reads as a missing-child alert."""
    record_scan(conn, student, at(7, 0), config)
    result = close_open_days(conn, "2026-09-01", config)

    assert result.absent == 0
    assert result.exit_missing == 1
    flags = conn.execute("SELECT flags FROM attendance_days").fetchone()[0]
    assert "exit_missing" in flags
    # No notification rows exist -- the queue is only written by the notify layer,
    # and close_open_days returns exit_missing separately from absences precisely
    # so the caller cannot accidentally notify on it.


def test_complete_day_is_left_alone(conn, student, config):
    record_scan(conn, student, at(7, 0), config)
    record_scan(conn, student, at(16, 0), config)
    result = close_open_days(conn, "2026-09-01", config)
    assert (result.absent, result.exit_missing) == (0, 0)


def test_absent_ids_are_returned_not_just_counted(conn, make_student, config):
    """The caller has to queue one notification per student; a count cannot do that."""
    first = make_student()
    second = make_student()
    result = close_open_days(conn, "2026-09-01", config)
    assert set(result.absent_ids) == {first, second}


def test_suspended_day_marks_nobody_absent(conn, make_student, config):
    """The worst thing this job could do: mark a whole roster absent on a day with
    no classes and text every guardian about it."""
    make_student()
    make_student()
    suspend_day(conn, "2026-09-01", "Typhoon signal no. 2", config)

    result = close_open_days(conn, "2026-09-01", config)

    assert result.absent_ids == ()
    assert "Typhoon" in result.skipped
    assert conn.execute("SELECT COUNT(*) FROM attendance_days").fetchone()[0] == 0


def test_closing_twice_adds_no_second_absence(conn, make_student, config):
    make_student()
    close_open_days(conn, "2026-09-01", config)
    second = close_open_days(conn, "2026-09-01", config)

    assert second.absent == 0
    assert conn.execute("SELECT COUNT(*) FROM attendance_days").fetchone()[0] == 1
