"""Grouping and packing rules for guardian coalescing.

Two constraints that an end-to-end run exposed and unit tests had missed:

1. Coalescing must be bounded by when the EVENT happened, not just by which rows
   happen to be pending. Merging a 7am arrival with a 4pm departure produces a
   message that is wrong for both. This matters most in exactly the case coalescing
   exists to survive -- a morning network outage that flushes at 4pm.

2. Overflow must SPLIT into a second message, never truncate. Truncating silently
   deleted a child's departure from the middle of a sibling message. A parent
   cannot act on information that was quietly dropped.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from . import gsm7

PREFIX = "TRACKIFY: "


def group_rows(
    rows: list[sqlite3.Row], window: timedelta
) -> list[list[sqlite3.Row]]:
    """Split one guardian's pending rows into event-time-adjacent clusters.

    Rows are clustered greedily: a row joins the current cluster only if its event
    is within `window` of the cluster's first event.
    """
    ordered = sorted(rows, key=lambda r: (r["event_at"], r["id"]))
    clusters: list[list[sqlite3.Row]] = []
    current: list[sqlite3.Row] = []
    anchor: datetime | None = None

    for row in ordered:
        event_at = datetime.fromisoformat(row["event_at"])
        if anchor is None or event_at - anchor <= window:
            if anchor is None:
                anchor = event_at
            current.append(row)
        else:
            clusters.append(current)
            current = [row]
            anchor = event_at

    if current:
        clusters.append(current)
    return clusters


def _fragment(row: sqlite3.Row) -> str:
    """One child's part, with the shared prefix removed."""
    return row["body"].replace(PREFIX, "", 1).rstrip(". ")


def render_group(rows: list[sqlite3.Row]) -> str:
    if len(rows) == 1:
        return rows[0]["body"]
    return PREFIX + ". ".join(_fragment(r) for r in rows) + "."


def pack(rows: list[sqlite3.Row]) -> list[list[sqlite3.Row]]:
    """Split a cluster into the fewest messages that each fit one SMS segment.

    Returns lists of rows; each list renders to a body within 160 septets.
    A single row whose own body overflows is returned alone -- render_group falls
    back to that row's already-validated body.
    """
    packed: list[list[sqlite3.Row]] = []
    current: list[sqlite3.Row] = []

    for row in rows:
        candidate = current + [row]
        if current and gsm7.septets(render_group(candidate)) > gsm7.SINGLE_SEGMENT:
            packed.append(current)
            current = [row]
        else:
            current = candidate

    if current:
        packed.append(current)
    return packed


def plan_messages(
    rows: list[sqlite3.Row], window: timedelta
) -> list[tuple[list[sqlite3.Row], str]]:
    """Full pipeline: cluster by event time, then pack each cluster to fit.

    Returns (rows, body) pairs, one per outbound message.
    """
    messages: list[tuple[list[sqlite3.Row], str]] = []
    for cluster in group_rows(rows, window):
        for chunk in pack(cluster):
            messages.append((chunk, render_group(chunk)))
    return messages
