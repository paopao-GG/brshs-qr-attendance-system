"""Linear regression on the attendance trend.

docs/analytics-model.md section 2. This model answers exactly one question: is attendance
across the school rising or falling over the observation period?

    y_hat = a + b*x        x = school-day index,  y = that day's attendance RATE

Two traps section 2 calls out, both enforced here rather than left to discipline.

**y is the daily rate, never a running total.** Regressing a cumulative figure on a day
index fits at R^2 around 0.99 with a slope that is just the mean rate restated. It looks
like the strongest result in the study and carries no information at all. daily_rates()
returns per-day rates and nothing in this module accumulates them.

**Consecutive school days are not independent observations.** OLS assumes they are, so
the p-value on the slope is optimistic when they are autocorrelated. Durbin-Watson is
therefore computed every time and reported alongside, never on request. Saying the
assumption is strained is a stronger result than not looking.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

# Below this a line cannot be fitted with any residual degrees of freedom: OLS needs
# n - 2 >= 1 for a confidence interval on the slope.
MIN_DAYS = 3
# Section 2: "With 20 points, statistical power is low." Under ten it is lower still, and
# a slope from six days should not be read as a trend without that said out loud.
LOW_POWER_BELOW = 10
# Durbin-Watson near 2 means no autocorrelation. This is the conventional band outside
# which the independence assumption is doing visible work.
DW_OK = (1.5, 2.5)

# Statuses that count as attending. Online participation counts as present, per
# analytics-model.md section 1 and corrections.TYPE_STATUS.
PRESENT = ("present", "late", "online")
# Excused days leave the DENOMINATOR rather than counting as absent -- the same rule the
# register and the XLSX export already apply.
COUNTED = PRESENT + ("absent",)


@dataclass(frozen=True)
class Insufficient:
    """Not enough data, and precisely what is missing.

    A separate type rather than None or a zeroed Trend. A zero slope reported into a
    spreadsheet reads as "attendance is flat"; this reads as "nothing has been measured
    yet", which is the truth and is actionable.
    """

    reason: str
    n: int = 0
    needed: int = MIN_DAYS

    @property
    def ok(self) -> bool:
        return False


@dataclass(frozen=True)
class Trend:
    n: int
    slope: float
    intercept: float
    r_squared: float
    p_value: float
    ci_low: float
    ci_high: float
    durbin_watson: float
    days: list[tuple[str, float]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return True

    @property
    def low_power(self) -> bool:
        return self.n < LOW_POWER_BELOW

    @property
    def autocorrelated(self) -> bool:
        return not (DW_OK[0] <= self.durbin_watson <= DW_OK[1])

    @property
    def direction(self) -> str:
        if self.p_value > 0.05:
            return "no significant trend"
        return "rising" if self.slope > 0 else "falling"

    @property
    def caveats(self) -> list[str]:
        """Everything that has to travel with this result."""
        notes = []
        if self.low_power:
            notes.append(
                f"Low statistical power: {self.n} school days. Read the 95% confidence "
                "interval, not the slope alone."
            )
        if self.autocorrelated:
            notes.append(
                f"Durbin-Watson {self.durbin_watson:.2f} is outside "
                f"{DW_OK[0]}-{DW_OK[1]}, so consecutive days are autocorrelated and OLS's "
                "independence assumption does not hold. The p-value on the slope is "
                "optimistic; report it as indicative, not conclusive."
            )
        else:
            notes.append(
                f"Durbin-Watson {self.durbin_watson:.2f} is close to 2: no evidence of "
                "autocorrelation between consecutive school days."
            )
        return notes


def daily_rates(conn: sqlite3.Connection, *, section_id: int | None = None,
                start: str | None = None, end: str | None = None
                ) -> list[tuple[str, float]]:
    """(date, attendance rate) per school day, oldest first.

    The rate is present+late+online over those plus absent. Excused days leave the
    denominator entirely -- a student with a medical certificate is neither attending nor
    truant, and counting them either way misstates the day.

    Live rows only: a corrected day is counted once, at its corrected value.
    """
    sql = ["""SELECT a.date AS date,
                     SUM(CASE WHEN a.status IN ('present','late','online')
                              THEN 1 ELSE 0 END) AS attended,
                     SUM(CASE WHEN a.status IN ('present','late','online','absent')
                              THEN 1 ELSE 0 END) AS eligible
              FROM attendance_days a
              JOIN students s ON s.id = a.student_id
              WHERE a.superseded_by IS NULL"""]
    params: list = []
    if section_id is not None:
        sql.append("AND s.section_id = ?")
        params.append(section_id)
    if start:
        sql.append("AND a.date >= ?")
        params.append(start)
    if end:
        sql.append("AND a.date <= ?")
        params.append(end)
    sql.append("GROUP BY a.date ORDER BY a.date")

    rows = conn.execute(" ".join(sql), params).fetchall()
    # A day where every student was excused has an undefined rate, not a zero one. It is
    # dropped rather than fitted as 0%, which would drag the slope down for a day nobody
    # was expected in.
    return [(r["date"], r["attended"] / r["eligible"])
            for r in rows if r["eligible"]]


def attendance_trend(conn: sqlite3.Connection, *, section_id: int | None = None,
                     start: str | None = None, end: str | None = None
                     ) -> Trend | Insufficient:
    """Fit the daily attendance rate against the school-day index."""
    series = daily_rates(conn, section_id=section_id, start=start, end=end)
    n = len(series)

    if n < MIN_DAYS:
        where = "this section" if section_id is not None else "the database"
        return Insufficient(
            reason=(f"A trend line needs at least {MIN_DAYS} school days with attendance "
                    f"recorded; {where} has {n}. Scan students for a few days, then "
                    "export again."),
            n=n,
        )

    rates = [rate for _, rate in series]
    if len(set(rates)) == 1:
        # Every day identical. OLS returns slope 0 with an undefined p-value here, and
        # "no variation to explain" is more useful than a NaN in a spreadsheet.
        return Insufficient(
            reason=(f"Every one of the {n} school days has an identical attendance rate "
                    f"of {rates[0]:.1%}, so there is no variation for a trend to "
                    "describe. This usually means only one day of scanning has been "
                    "closed out, or every student shares a status."),
            n=n, needed=MIN_DAYS,
        )

    import numpy as np
    import statsmodels.api as sm
    from statsmodels.stats.stattools import durbin_watson

    # x is the school-day INDEX, 1..n, not a calendar date: section 2 regresses on the
    # ordinal position of the school day, so weekends and holidays do not open gaps.
    x = sm.add_constant(np.arange(1, n + 1, dtype=float))
    y = np.asarray(rates, dtype=float)

    model = sm.OLS(y, x).fit()
    intercept, slope = model.params
    ci_low, ci_high = model.conf_int(alpha=0.05)[1]

    return Trend(
        n=n,
        slope=float(slope),
        intercept=float(intercept),
        r_squared=float(model.rsquared),
        p_value=float(model.pvalues[1]),
        ci_low=float(ci_low),
        ci_high=float(ci_high),
        durbin_watson=float(durbin_watson(model.resid)),
        days=series,
    )
