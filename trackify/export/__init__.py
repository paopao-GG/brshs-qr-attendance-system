"""Export helpers.

`safe_filename` lives here rather than in each exporter because all three were carrying
their own identical copy of the same comprehension, and a filename rule that differs
between the register, the SF2 and the analytics workbook is a bug waiting for the first
section somebody names with a slash.
"""

from __future__ import annotations

# Excel refuses / \ : * ? " < > | in a filename, and a section named by a teacher is
# user input that will eventually contain one of them. Everything outside this set
# collapses to a hyphen rather than being dropped, so two sections cannot silently
# produce the same file.
KEEP = "-_"


def safe_filename(label: str) -> str:
    """A section label, grade or scope reduced to something a filesystem accepts."""
    return "".join(c if c.isalnum() or c in KEEP else "-" for c in label)
