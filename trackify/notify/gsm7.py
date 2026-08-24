"""GSM-7 alphabet validation.

An SMS in the GSM-7 default alphabet fits 160 characters. A single character outside
it forces the whole message to UCS-2, which fits only 70 -- so the message silently
splits and bills double.

Note what is NOT a problem: n-tilde, a-umlaut, e-acute and friends ARE in GSM-7, so
Filipino surnames like Pena and Munoz are safe. The real traps are punctuation from a
word processor (smart quotes, em dashes) and the peso sign, which GSM-7 lacks despite
having the pound, dollar and yen.
"""

from __future__ import annotations

# GSM 03.38 basic character set.
BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅå"
    "Δ_ΦΓΛΩΠΨΣΘΞ"
    "ÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)

# Extension table: each of these occupies TWO septets, not one.
EXTENDED = "^{}\\[~]|€"

BASIC_SET = frozenset(BASIC)
EXTENDED_SET = frozenset(EXTENDED)

SINGLE_SEGMENT = 160
UCS2_SEGMENT = 70

# Characters that show up in practice, with the reason, so the error is actionable.
COMMON_TRAPS = {
    "‘": "left single quote (smart quote from Word)",
    "’": "right single quote / apostrophe (smart quote from Word)",
    "“": "left double quote (smart quote from Word)",
    "”": "right double quote (smart quote from Word)",
    "–": "en dash (Word autocorrect of '-')",
    "—": "em dash (Word autocorrect of '--')",
    "₱": "peso sign (GSM-7 has GBP/USD/JPY but not PHP -- write 'PHP 500')",
    "…": "ellipsis character (use three periods)",
}


class NotGSM7(ValueError):
    def __init__(self, offenders: list[tuple[str, str]]) -> None:
        self.offenders = offenders
        detail = "; ".join(f"{char!r}: {why}" for char, why in offenders)
        super().__init__(f"Message contains non-GSM-7 characters -- {detail}")


def offenders(text: str) -> list[tuple[str, str]]:
    """Return (character, explanation) for every character outside GSM-7."""
    found: dict[str, str] = {}
    for char in text:
        if char in BASIC_SET or char in EXTENDED_SET:
            continue
        found.setdefault(
            char, COMMON_TRAPS.get(char, f"not in the GSM-7 alphabet (U+{ord(char):04X})")
        )
    return list(found.items())


def is_gsm7(text: str) -> bool:
    return not offenders(text)


def septets(text: str) -> int:
    """Length in septets. Extension-table characters count double."""
    return sum(2 if c in EXTENDED_SET else 1 for c in text)


def segments(text: str) -> int:
    """How many SMS this body will bill as."""
    if not is_gsm7(text):
        length = len(text)
        return 1 if length <= UCS2_SEGMENT else -(-length // 67)
    length = septets(text)
    return 1 if length <= SINGLE_SEGMENT else -(-length // 153)


def validate(text: str, *, max_segments: int = 1) -> None:
    """Raise if the body is not GSM-7 or would bill as more than max_segments.

    Called before a row is written to the queue, so a bad template fails at
    enqueue time rather than silently costing double on every send.
    """
    bad = offenders(text)
    if bad:
        raise NotGSM7(bad)
    count = segments(text)
    if count > max_segments:
        raise ValueError(
            f"Message is {septets(text)} septets and would bill as {count} messages "
            f"(limit {max_segments}). Shorten it."
        )


def truncate(text: str, limit: int = SINGLE_SEGMENT) -> str:
    """Deterministically shorten to fit one segment, ending with a single period.

    Used for coalesced multi-child messages, which are the bodies most likely to
    overflow.
    """
    if septets(text) <= limit:
        return text
    out: list[str] = []
    used = 0
    for char in text:
        cost = 2 if char in EXTENDED_SET else 1
        if used + cost > limit - 1:
            break
        out.append(char)
        used += cost
    return "".join(out).rstrip(" ,.;") + "."
