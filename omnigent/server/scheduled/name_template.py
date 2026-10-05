"""Validate and render date placeholders in automation session names."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

_TOKENS = ("YYYY", "MMMM", "MMM", "MM", "Mon", "DD", "dddd", "ddd", "HH", "mm")
_SUPPORTED = "supported tokens (case-sensitive): " + " ".join(_TOKENS)
_RUNS = re.compile(r"[A-Za-z]+|[ /.:,_-]+")
_CLOCK_HINT = "mm is minutes and needs HH (e.g. {{HH:mm}}); MM is the month"
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class NameTemplateError(ValueError):
    """An automation name contains an invalid date placeholder."""


def _pattern(body: str) -> tuple[str, ...]:
    body = body.strip(" \t")
    runs = tuple(_RUNS.findall(body))
    tokens = runs[::2]
    if "".join(runs) != body or len(runs) % 2 == 0 or any(t not in _TOKENS for t in tokens):
        raise NameTemplateError(_SUPPORTED)
    # Silent misreads: mm (minutes) used as a month, or MM (month) after HH: as minutes.
    clock_typo = any(
        runs[i] == "HH" and ":" in runs[i + 1] and runs[i + 2] == "MM"
        for i in range(0, len(runs) - 2, 2)
    )
    if clock_typo or ("mm" in tokens and "HH" not in tokens):
        raise NameTemplateError(_CLOCK_HINT)
    return runs


def _parse(name: str) -> list[str | tuple[str, ...]]:
    if len(name) > 256:
        raise NameTemplateError("templated names must be at most 256 Unicode code points")
    parts: list[str | tuple[str, ...]] = []
    index = 0
    while index < len(name):
        if name.startswith(r"\{{", index):
            parts.append("{{")
            index += 3
        elif name.startswith("{{", index):
            closing = name.find("}}", index + 2)
            if closing == -1:
                raise NameTemplateError("unclosed placeholder; " + _SUPPORTED)
            parts.append(_pattern(name[index + 2 : closing]))
            index = closing + 2
        else:
            parts.append(name[index])
            index += 1
    return parts


def validate_name_template(name: str) -> None:
    """Validate templated names, leaving plain names unrestricted."""
    if "{{" in name:
        _parse(name)


def render_session_name(name: str, at_epoch: int, tz_name: str) -> str:
    """Render a whole name at worker start in the task timezone, in English."""
    if "{{" not in name:
        return name
    parts = _parse(name)
    instant = datetime.fromtimestamp(at_epoch, ZoneInfo(tz_name))
    month = _MONTHS[instant.month - 1]
    weekday = _WEEKDAYS[instant.weekday()]
    values = {
        "YYYY": f"{instant.year:04d}",
        "MMMM": month,
        "MMM": month[:3],
        "Mon": month[:3],
        "MM": f"{instant.month:02d}",
        "DD": f"{instant.day:02d}",
        "dddd": weekday,
        "ddd": weekday[:3],
        "HH": f"{instant.hour:02d}",
        "mm": f"{instant.minute:02d}",
    }
    # Tokens expand by at most 9/4, so 256 code points render to at most 576 (<768).
    return "".join(
        part if isinstance(part, str) else "".join(values.get(run, run) for run in part)
        for part in parts
    )
