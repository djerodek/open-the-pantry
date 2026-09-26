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
    # Range-checked: "00:99:99" and "20000:00:00" used to be accepted, and
    # the time filter then built one entry per 20 minutes up to that value
    # -- 1.4 million entries, ~90 MB, and a stalled UI at 20,000 days.
    if minutes >= 60 or (match.group(1) is not None and hours >= 24):
        return None
    total = days * 24 * 60 + hours * 60 + minutes
    return total if total <= MAX_COOK_MINUTES else None


# 60 days: covers long cures and ferments (the smoked-bacon recipe is 8 days).
MAX_COOK_MINUTES = 60 * 24 * 60


def ddhhmm_error(value) -> str | None:
    """Why a dd:hh:mm value is rejected, for the API's 422 message; None if fine."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str) or not DDHHMM_RE.match(value):
        return "Use dd:hh:mm or hh:mm, e.g. 00:01:30."
    if ddhhmm_to_minutes(value) is None:
        return "Hours must be under 24 and minutes under 60, up to 60 days in total."
    return None


def minutes_to_ddhhmm(total_minutes: int | None) -> str | None:
    if total_minutes is None:
        return None
    days, rem = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rem, 60)
    return f"{days:02d}:{hours:02d}:{minutes:02d}"


def minutes_to_label(total_minutes: int) -> str:
    """Human label for a bucket ceiling, e.g. 80 -> '1h 20m', 120 -> '2h', 40 -> '40m'."""
    if total_minutes >= 24 * 60:
        days, rem = divmod(total_minutes, 24 * 60)
        return f"{days}d {rem // 60}h" if rem // 60 else f"{days}d"
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

    # 20-minute steps up to a day; whole days beyond that. A 20-minute grid
    # over an 8-day cure would be 576 options nobody scrolls through, and
    # an unbounded value made it unbounded (see ddhhmm_to_minutes).
    ceiling = min(ceiling, MAX_COOK_MINUTES)
    steps = list(range(20, min(ceiling, 24 * 60) + 1, 20))
    if ceiling > 24 * 60:
        steps += list(range(2 * 24 * 60, ceiling + 24 * 60, 24 * 60))
    return [
        {"minutes": m, "label": minutes_to_label(m), "hour_group": (m - 1) // 60}
        for m in steps
    ]
