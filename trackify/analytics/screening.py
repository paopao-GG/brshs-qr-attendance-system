"""Descriptive statistics for the metal-detector screening procedure.

docs/prohibited-items.md section 9 and hardware.md section 8. These numbers are
DESCRIPTIVE and are deliberately not fed into the risk score: over a 20-day study you may
record zero, one or two incidents, and a criterion with almost no variance contributes
noise to a composite, cannot be validated, and invites the obvious question of how a
weight was derived for something that essentially never happened.

The detector is a separate device operated by a person, so there is no sensor reading
anywhere here. Every figure counts a human judgement, which is why the useful metrics are
procedural -- coverage, alarm rate, confirmation rate -- rather than the ROC curve an
instrumented detector would have supported.

**No student is named in any of this.** incidents.visibility defaults to 'restricted'
because a record naming a minor beside a description of a prohibited item is sensitive
personal information under RA 10173. Everything below is a count.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

OUTCOMES = ("clear", "common_items", "prohibited", "school_hazard",
            "pending_verification", "not_screened", "overridden")

CATEGORIES = ("bladed", "blunt", "pointed", "tool", "other")

# The outcomes that mean a person looked and found something worth recording. Used for
# the confirmation rate: of the bags flagged by the detector, how many held something.
CONFIRMED = ("prohibited", "school_hazard")


@dataclass(frozen=True)
class ScreeningSummary:
    scans: int = 0
    screened: int = 0
    alarms: int = 0                     # metal_detected = 1
    confirmed: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    incidents_by_category: dict[str, int] = field(default_factory=dict)
    incidents_by_severity: dict[int, int] = field(default_factory=dict)
    incident_total: int = 0
    severity_total: int = 0
    custody_by_status: dict[str, int] = field(default_factory=dict)
    custody_total: int = 0
    released_unbacked: int = 0
    hazard_requests: int = 0

    @property
    def ok(self) -> bool:
        return self.scans > 0

    @property
    def coverage(self) -> float | None:
        """Share of arriving students who were screened at all.

        hardware.md section 8's headline procedure metric. Silence is 'not_screened',
        never 'clear', so this is a real measure rather than an artefact of the UI.
        """
        return self.screened / self.scans if self.scans else None

    @property
    def alarm_rate(self) -> float | None:
        return self.alarms / self.screened if self.screened else None

    @property
    def confirmation_rate(self) -> float | None:
        """Of the bags the detector flagged, how many actually held something.

        The closest available stand-in for precision now that the detector is an
        off-the-shelf device with no readings to threshold.
        """
        return self.confirmed / self.alarms if self.alarms else None

    @property
    def notes(self) -> list[str]:
        out = []
        # The custody warnings come first and are NOT gated on there being scans. A
        # released-unbacked item is a control failure, and suppressing it because the
        # date range happens to hold no scans is exactly when it would go unseen.
        if self.released_unbacked:
            out.append(
                f"{self.released_unbacked} custody item(s) were released with no hazard "
                "request on file. The request is the control; releasing without one is "
                "the exception worth reviewing."
            )
        if not self.scans:
            out.append("No scans recorded, so no screening has taken place yet.")
            return out
        if not self.screened:
            out.append("Scans exist but no screening was answered. Coverage is 0%.")
        if self.outcomes.get("not_screened"):
            out.append(
                f"{self.outcomes['not_screened']} scan(s) recorded 'not screened'. That "
                "is a deliberate outcome a person chose, not a gap in the data."
            )
        if self.outcomes.get("overridden"):
            out.append(
                f"{self.outcomes['overridden']} screening(s) were overridden. Each "
                "carries a reason in screening_events.override_reason."
            )
        if self.incident_total and self.incident_total < 5:
            out.append(
                f"Only {self.incident_total} incident(s) recorded. Report these "
                "descriptively; the count is far too small to model or to weight."
            )
        return out


def _counts(conn: sqlite3.Connection, sql: str, params=()) -> dict:
    return {row[0]: row[1] for row in conn.execute(sql, params)}


def summarise(conn: sqlite3.Connection, *, start: str | None = None,
              end: str | None = None) -> ScreeningSummary:
    """Every screening figure for a date range. Counts only, never names."""
    where, params = [], []
    if start:
        where.append("date >= ?")
        params.append(start)
    if end:
        where.append("date <= ?")
        params.append(end)
    scan_filter = (" WHERE " + " AND ".join(where)) if where else ""

    scans = conn.execute(
        f"SELECT COUNT(*) FROM scan_events{scan_filter}", params).fetchone()[0]

    # screening_events has no date column of its own -- attribution flows through the
    # arming scan (flow.md Rule 2), so the range is applied there.
    join = """FROM screening_events e JOIN scan_events sc ON sc.id = e.scan_event_id"""
    ev_where, ev_params = [], []
    if start:
        ev_where.append("sc.date >= ?")
        ev_params.append(start)
    if end:
        ev_where.append("sc.date <= ?")
        ev_params.append(end)
    ev_filter = (" WHERE " + " AND ".join(ev_where)) if ev_where else ""

    outcomes = _counts(
        conn, f"SELECT e.outcome, COUNT(*) {join}{ev_filter} GROUP BY e.outcome",
        ev_params)
    screened = sum(outcomes.values())
    alarms = conn.execute(
        f"SELECT COUNT(*) {join}{ev_filter}"
        f"{' AND' if ev_filter else ' WHERE'} e.metal_detected = 1", ev_params
    ).fetchone()[0]
    confirmed = sum(outcomes.get(name, 0) for name in CONFIRMED)

    inc_where, inc_params = [], []
    if start:
        inc_where.append("occurred_at >= ?")
        inc_params.append(start)
    if end:
        inc_where.append("occurred_at <= ?")
        inc_params.append(end + "T23:59:59")
    inc_filter = (" WHERE " + " AND ".join(inc_where)) if inc_where else ""

    by_category = _counts(
        conn, f"SELECT category, COUNT(*) FROM incidents{inc_filter} GROUP BY category",
        inc_params)
    by_severity = _counts(
        conn, f"SELECT severity, COUNT(*) FROM incidents{inc_filter} GROUP BY severity",
        inc_params)
    severity_total = conn.execute(
        f"SELECT COALESCE(SUM(severity), 0) FROM incidents{inc_filter}",
        inc_params).fetchone()[0]

    custody = _counts(
        conn, "SELECT status, COUNT(*) FROM custody_items GROUP BY status")
    unbacked = conn.execute(
        "SELECT COUNT(*) FROM custody_items WHERE released_unbacked = 1").fetchone()[0]
    requests = conn.execute("SELECT COUNT(*) FROM hazard_requests").fetchone()[0]

    return ScreeningSummary(
        scans=scans, screened=screened, alarms=alarms, confirmed=confirmed,
        outcomes={name: outcomes.get(name, 0) for name in OUTCOMES},
        incidents_by_category={name: by_category.get(name, 0) for name in CATEGORIES},
        incidents_by_severity={level: by_severity.get(level, 0) for level in (1, 2, 3, 4)},
        incident_total=sum(by_category.values()),
        severity_total=severity_total,
        custody_by_status=custody,
        custody_total=sum(custody.values()),
        released_unbacked=unbacked,
        hazard_requests=requests,
    )
