"""Philippine mobile number normalisation.

Everything is stored as 639XXXXXXXXX, and prefixed with + for AT+CMGS.
Accepting the three common written forms at import time avoids a class of silent
delivery failures where a valid number was rejected for its formatting.
"""

from __future__ import annotations

import re

STORED_LENGTH = 12  # 63 + 9XXXXXXXXX
_NON_DIGIT = re.compile(r"[^\d+]")


class InvalidMobile(ValueError):
    pass


def normalise(raw: str | None) -> str | None:
    """Return 639XXXXXXXXX, or None when the field is genuinely empty.

    Raises InvalidMobile when a value is present but is not a PH mobile number.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    cleaned = _NON_DIGIT.sub("", text)
    if not cleaned:
        # Had content but no digits at all -- "N/A", "none", "wala". This is a data
        # error the operator must see, not an absent number. Treating it as absent
        # would silently exclude the student from every notification.
        raise InvalidMobile(f"Not a Philippine mobile number: {raw!r}")

    if cleaned.startswith("+"):
        cleaned = cleaned[1:]

    if cleaned.startswith("09") and len(cleaned) == 11:
        cleaned = "63" + cleaned[1:]
    elif cleaned.startswith("639") and len(cleaned) == STORED_LENGTH:
        pass
    elif cleaned.startswith("9") and len(cleaned) == 10:
        cleaned = "63" + cleaned
    else:
        raise InvalidMobile(f"Not a Philippine mobile number: {raw!r}")

    if len(cleaned) != STORED_LENGTH or not cleaned.startswith("639"):
        raise InvalidMobile(f"Not a Philippine mobile number: {raw!r}")
    return cleaned


def for_display(stored: str | None) -> str:
    """Render 639171234567 as 0917 123 4567 for the UI."""
    if not stored or len(stored) != STORED_LENGTH:
        return stored or ""
    local = "0" + stored[2:]
    return f"{local[:4]} {local[4:7]} {local[7:]}"
