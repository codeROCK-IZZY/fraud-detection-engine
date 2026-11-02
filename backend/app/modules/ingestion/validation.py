"""Input validation helpers, including detection of raw card numbers (PANs).

This system must only ever see card *tokens*. A 13–19 digit number that passes
the Luhn checksum is almost certainly a real card number, so it is rejected at
ingestion and blocked from free-text case notes.
"""

from __future__ import annotations

import re

_PAN_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def looks_like_pan(value: str) -> bool:
    digits = re.sub(r"[ -]", "", value)
    return digits.isdigit() and 13 <= len(digits) <= 19 and luhn_ok(digits)


def contains_pan(text: str) -> bool:
    return any(looks_like_pan(m.group(0)) for m in _PAN_CANDIDATE.finditer(text))
