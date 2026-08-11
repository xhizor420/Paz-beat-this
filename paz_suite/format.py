"""Human-readable formatting for durations, sizes and e621 scores."""

from __future__ import annotations


def fmt_time(sec: float) -> str:
    if sec is None or sec < 0:
        return "--"
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {(sec % 3600) // 60:02d}m"


def fmt_clock(sec: float) -> str:
    """Timecode for a scrubber: H:MM:SS.d or M:SS.d"""
    if sec is None or sec < 0:
        sec = 0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    if h:
        return f"{h}:{m:02d}:{s:04.1f}"
    return f"{m}:{s:04.1f}"


def fmt_len(sec) -> str:
    """Durations the way a person says them: 30 sec, 1 min, 1 min 12 sec."""
    if sec is None or sec <= 0:
        return "--"
    sec = int(round(sec))
    if sec < 60:
        return f"{sec} sec"
    minutes, s = divmod(sec, 60)
    if minutes < 60:
        return f"{minutes} min {s} sec" if s else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} hr {minutes} min" if minutes else f"{hours} hr"


def fmt_size(n) -> str:
    if not n:
        return "--"
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{int(n)} B"


def fmt_score(n) -> str:
    """e621 score, compact: 84, 1.2k, 14k."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n == 0:
        return ""
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 10000:
        return f"{sign}{n // 1000}k"
    if n >= 1000:
        return f"{sign}{n / 1000:.1f}k".replace(".0k", "k")
    return f"{sign}{n}"
