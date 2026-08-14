"""Parsing snooze durations.

Deliberately narrow: a misread duration silently changes how long a session is
protected from reaping, so anything ambiguous is refused rather than
interpreted. No compound forms, no fractions, no zero, no negatives.
"""

from __future__ import annotations

import re
from datetime import timedelta

_PATTERN = re.compile(r"^(\d+)([mhd])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_duration(text: str) -> timedelta:
    match = _PATTERN.match(text.strip())
    if not match:
        raise ValueError(f"cannot read duration {text!r}; expected forms like 30m, 4h, 2d")
    amount = int(match.group(1))
    if amount == 0:
        raise ValueError("duration must be greater than zero; use unsnooze to clear one")
    return timedelta(**{_UNITS[match.group(2)]: amount})
