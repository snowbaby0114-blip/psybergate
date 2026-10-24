"""The only place where UTC instants and SAST business dates are converted into each other.

Instants are timezone-aware datetimes; business days are calendar dates in Africa/Johannesburg.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Africa/Johannesburg")
WINDOW_DAYS = 30


def ensure_utc(dt: datetime) -> datetime:
    if dt.utcoffset() is None:
        raise ValueError("datetime must carry a UTC offset; a naive datetime is ambiguous")
    return dt.astimezone(UTC)


def to_local_date(instant: datetime) -> date:
    # ensure_utc rejects naive datetimes, which astimezone would silently read as machine-local.
    return ensure_utc(instant).astimezone(TZ).date()


def local_day_start_utc(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=TZ).astimezone(UTC)


def window_start_date(d: date) -> date:
    return d - timedelta(days=WINDOW_DAYS - 1)


def window_bounds_utc(d: date) -> tuple[datetime, datetime]:
    """Half-open `[start, end)` UTC interval of the 30 business days ending on `d`."""
    return local_day_start_utc(window_start_date(d)), local_day_start_utc(d + timedelta(days=1))


def history_bounds_utc(d: date) -> tuple[datetime, datetime]:
    """Half-open UTC interval holding every recorded dispense that a dispense on `d` can
    interact with: its own window (back to d-29) and the windows of later dispenses (up to d+29).
    """
    return (
        local_day_start_utc(window_start_date(d)),
        local_day_start_utc(d + timedelta(days=WINDOW_DAYS)),
    )
