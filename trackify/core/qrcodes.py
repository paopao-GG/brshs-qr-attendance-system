"""QR payload encoding, signed so codes cannot be forged.

The research plan names proxy attendance as a problem manual systems suffer from.
A QR holding a bare sequential number is forged by incrementing it, which would
reproduce the exact failure this system claims to fix. Every payload therefore carries
a truncated HMAC.

    TRK-{lrn}-{hmac8}

THE SIGNED NUMBER IS THE LRN, NOT THE students.id ROW ID, and the distinction is the
whole reason a card keeps working. A printed card outlives any one database: reseeding
renumbers every row, so a payload keyed on a row id would silently invalidate a box of
cards that still look perfectly valid. The LRN is the student's identity at DepEd and
does not move.

These parameters were called `student_id` until a review pointed out that no caller
passes one -- make_qr.py and seed_demo.py both pass `int(row["lrn"])`. A maintainer who
believed the old name and passed a row id would have printed exactly the box of dead
cards this design exists to prevent.

The secret lives in the environment (TRACKIFY_QR_SECRET), never in source.
Changing it invalidates every printed code.
"""

from __future__ import annotations

import hmac
import re
from hashlib import sha256

PREFIX = "TRK"
SIG_LEN = 8
_PATTERN = re.compile(rf"^{PREFIX}-(\d+)-([0-9a-f]{{{SIG_LEN}}})$")


class InvalidQRCode(ValueError):
    """Payload was malformed, unsigned, or signed with a different secret."""


def _signature(lrn: int, secret: str) -> str:
    if not secret:
        raise ValueError(
            "TRACKIFY_QR_SECRET is not set. Generate one with:\n"
            '  python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    mac = hmac.new(secret.encode("utf8"), str(lrn).encode("utf8"), sha256)
    return mac.hexdigest()[:SIG_LEN]


def encode(lrn: int, secret: str) -> str:
    """Build the payload printed into a student's QR code.

    `lrn` is the DepEd Learner Reference Number, not the students.id row id.
    """
    if lrn <= 0:
        raise ValueError("lrn must be positive")
    return f"{PREFIX}-{lrn}-{_signature(lrn, secret)}"


def decode(payload: str, secret: str) -> int:
    """Verify a scanned payload and return the LRN it carries.

    Raises InvalidQRCode for anything malformed or incorrectly signed. The caller looks
    the LRN up in students -- see ScanService.student_row.
    """
    match = _PATTERN.match((payload or "").strip())
    if not match:
        raise InvalidQRCode("Unrecognised code format")

    lrn = int(match.group(1))
    # Constant-time compare so a timing side channel cannot leak the signature.
    if not hmac.compare_digest(match.group(2), _signature(lrn, secret)):
        raise InvalidQRCode("Code signature does not match")
    return lrn


def is_wellformed(payload: str) -> bool:
    """Shape check only, no signature verification.

    Used by the kiosk to tell a scanner misfire apart from a genuine bad code.
    """
    return bool(_PATTERN.match((payload or "").strip()))
