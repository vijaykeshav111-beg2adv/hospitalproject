"""Collision-safe generation of the human-readable business codes.

Every ``*_code`` / ``*_number`` column in the schema is ``UNIQUE`` (MySQL enforces
it), so a random suffix must be verified against the database *before* the row is
inserted. A plain 4-digit random suffix collides far more often than people
expect (birthday paradox: with ~100 rows/day a clash is ~39% likely, and ~86% at
200/day), and when it does, MySQL raises ``IntegrityError 1062`` and the whole
request fails with a 500 - a booking, a prescription or an invoice is lost.

``unique_code()`` retries with a wider suffix until it finds a free code, so code
exhaustion can no longer break a business transaction.

Usage::

    from ..codes import unique_code
    from .. import models

    code = unique_code(db, prefix="APT", model=models.Appointment,
                       column=models.Appointment.appointment_code)
"""
from __future__ import annotations

import random
import string
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .security import utcnow

# All code columns are String(24): "APT-20261006-" is 13 chars, so a suffix of
# up to 10 digits still fits.
MAX_WIDTH = 8


def unique_code(
    db: Session,
    *,
    prefix: str,
    model: Any,
    column: Any,
    stamp_format: str = "%Y%m%d",
    width: int = 6,
    max_attempts: int = 25,
) -> str:
    """Return a code like ``APT-20261006-482913`` that is not used yet.

    The suffix starts at ``width`` digits and grows by one digit after every
    fifth failed attempt, which makes repeated collisions vanishingly unlikely.
    """
    stamp = utcnow().strftime(stamp_format)

    for attempt in range(max_attempts):
        suffix_width = min(width + attempt // 5, MAX_WIDTH)
        suffix = "".join(random.choices(string.digits, k=suffix_width))
        code = f"{prefix}-{stamp}-{suffix}"

        taken = db.scalar(select(model.id).where(column == code).limit(1))
        if taken is None:
            return code

    # Extremely unlikely: fall back to a unique integer stamp so the caller
    # still gets a usable code instead of an exception.
    return f"{prefix}-{stamp}-{utcnow().strftime('%H%M%S%f')[:9]}"
