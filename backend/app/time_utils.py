import re

DDHHMM_RE = re.compile(r"^\s*(?:(\d+):)?(\d+):(\d+)\s*$")


def ddhhmm_to_minutes(value) -> int | None:
    """Parse 'dd:hh:mm' or 'hh:mm' into total minutes. Returns None if
    blank or invalid. Accepts any type defensively -- the API boundary
    constrains this to str|None via Pydantic, but internal callers
    shouldn't get an AttributeError for passing the wrong thing."""
    if not isinstance(value, str) or not value.strip():
        return None
    match = DDHHMM_RE.match(value)
    if not match:
        return None
    days = int(match.group(1) or 0)
    hours = int(match.group(2) or 0)
    minutes = int(match.group(3) or 0)
    return days * 24 * 60 + hours * 60 + minutes


def minutes_to_ddhhmm(total_minutes: int | None) -> str | None:
    if total_minutes is None:
        return None
    days, rem = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rem, 60)
    return f"{days:02d}:{hours:02d}:{minutes:02d}"


def minutes_to_label(total_minutes: int) -> str:
    """Human label for a bucket ceiling, e.g. 80 -> '1h 20m', 120 -> '2h', 40 -> '40m'."""
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def available_time_buckets(logged_minutes: list[int]) -> list[dict]:
    """
    Build the list of selectable '<= X' time-filter buckets in 20-minute
    increments, limited to what the data actually supports: only up to the
    smallest 20-minute-multiple ceiling that covers the longest recipe
    currently logged. No buckets are offered beyond that (they'd be
    redundant -- same result set as the max bucket).

    Each bucket also carries which hour it falls in, so the UI can group
    them into a two-level hour -> 20-minute-increment drill-down.
    """
    if not logged_minutes:
        return []

    max_minutes = max(logged_minutes)
    ceiling = ((max_minutes + 19) // 20) * 20  # round up to next 20-min multiple
    if ceiling == 0:
        ceiling = 20

    buckets = []
    m = 20
    while m <= ceiling:
        buckets.append({
            "minutes": m,
            "label": minutes_to_label(m),
            "hour_group": (m - 1) // 60,  # 0 for <=60, 1 for 61-120, etc.
        })
        m += 20
    return buckets
