"""Calendar vocabulary shared by core, notify, ui and export.

MONTHS was declared identically in four files -- export/sf2.py, export/xlsx.py,
notify/periodic.py and ui/records.py. Four copies of a twelve-element tuple is not a
correctness risk on its own; it is a signal that nobody owns the calendar, and the next
thing that wants a month name makes a fifth.

core/ is the only package all four can import from without a cycle.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date as Date

MONTHS = ("January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December")


def month_name(month: int) -> str:
    """1-based, like every DepEd form and every date object."""
    return MONTHS[month - 1]


def month_days(year: int, month: int) -> list[str]:
    """Every date in the month as an ISO string, in order."""
    return [
        Date(year, month, day).isoformat()
        for day in range(1, monthrange(year, month)[1] + 1)
    ]
