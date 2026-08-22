"""Shared application-level validation for the per-channel EPG time offset.

``Channel.epg_time_offset_minutes`` is a display shift in minutes: positive
means the channel airs programmes later than the EPG source's times. The
model field and its migration intentionally stay unconstrained; the ±1440
rule (one day either direction) is enforced here so every write surface —
the channel serializer, the bulk edit path (which bypasses model
validation) and the current-programs ``time_offset_minutes`` parameter —
applies one identical rule.
"""

import re

EPG_TIME_OFFSET_MIN_MINUTES = -1440
EPG_TIME_OFFSET_MAX_MINUTES = 1440

_EPG_TIME_OFFSET_ERROR = (
    "epg_time_offset_minutes must be an integer between "
    f"{EPG_TIME_OFFSET_MIN_MINUTES} and {EPG_TIME_OFFSET_MAX_MINUTES}"
)

_INT_RE = re.compile(r"^-?\d+$")


def validate_epg_time_offset_minutes(value):
    """Validate an EPG time offset value expressed in minutes.

    Returns the offset as ``int``. ``None`` and blank strings mean "no
    shift requested" and return ``None``. Raises ``ValueError`` for
    booleans, fractional numbers, malformed strings, and values outside
    the [-1440, 1440] range.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
        if not _INT_RE.match(value):
            raise ValueError(_EPG_TIME_OFFSET_ERROR)
        value = int(value)
    # bool is a subclass of int and must be rejected explicitly; floats
    # (including integral ones) and any other numeric types are rejected.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(_EPG_TIME_OFFSET_ERROR)
    if not EPG_TIME_OFFSET_MIN_MINUTES <= value <= EPG_TIME_OFFSET_MAX_MINUTES:
        raise ValueError(_EPG_TIME_OFFSET_ERROR)
    return value
