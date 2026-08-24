"""QR payload encoding, signed so codes cannot be forged.

The research plan names proxy attendance as a problem manual systems suffer from.
A QR holding a bare sequential student id is forged by incrementing a number, which
would reproduce the exact failure this system claims to fix. Every payload therefore
carries a truncated HMAC over the student id.

    TRK-{student_id}-{hmac8}

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


def _signature(student_id: int, secret: str) -> str:
    if not secret:
        raise ValueError(
            "TRACKIFY_QR_SECRET is not set. Generate one with:\n"
            '  python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    mac = hmac.new(secret.encode("utf8"), str(student_id).encode("utf8"), sha256)
    return mac.hexdigest()[:SIG_LEN]


def encode(student_id: int, secret: str) -> str:
    """Build the payload printed into a student's QR code."""
    if student_id <= 0:
        raise ValueError("student_id must be positive")
    return f"{PREFIX}-{student_id}-{_signature(student_id, secret)}"


def decode(payload: str, secret: str) -> int:
    """Verify a scanned payload and return the student id.

    Raises InvalidQRCode for anything malformed or incorrectly signed.
    """
    match = _PATTERN.match((payload or "").strip())
    if not match:
        raise InvalidQRCode("Unrecognised code format")

    student_id = int(match.group(1))
    # Constant-time compare so a timing side channel cannot leak the signature.
    if not hmac.compare_digest(match.group(2), _signature(student_id, secret)):
        raise InvalidQRCode("Code signature does not match")
    return student_id


def is_wellformed(payload: str) -> bool:
    """Shape check only, no signature verification.

    Used by the kiosk to tell a scanner misfire apart from a genuine bad code.
    """
    return bool(_PATTERN.match((payload or "").strip()))
