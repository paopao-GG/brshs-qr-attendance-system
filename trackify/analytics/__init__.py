"""Analytics for the research questions: attendance trend, per-student risk, screening.

    trend.attendance_trend(conn)        section 2  -- is attendance rising or falling?
    risk.compute(conn, config)          sections 4, 6, 7 -- who needs a look?
    ahp.active(conn)                    section 5  -- with what weights, and are they sound?
    screening.summarise(conn)           descriptive only, never scored

Everything here is read-only over the database except risk.compute(persist=True).
"""

from . import ahp, risk, screening, trend

__all__ = ["ahp", "risk", "screening", "trend"]
