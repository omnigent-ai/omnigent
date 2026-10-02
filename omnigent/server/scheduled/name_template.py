"""Validate and render date placeholders in automation session names."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

_TOKENS = ("YYYY", "MMMM", "MMM", "MM", "Mon", "DD", "dddd", "ddd", "HH", "mm")
_SUPPORTED = "supported tokens: " + " ".join(_TOKENS)
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
_HINTS = {
    "mon": "use MMM (or Mon)",
    "MON": "use MMM (or Mon)",
    "Month": "use MMMM",
    "Day": "use ddd / dddd",
    "Dy": "use ddd / dddd",
    "EEE": "use ddd / dddd",
    "EEEE": "use ddd / dddd",
    "yyyy": "use YYYY",
    "yy": "use YYYY",
    "YY": "use YYYY",
    "dd": "use DD or ddd",
    "d": "use DD or ddd",
    "D": "use DD or ddd",
    "DDD": "day of year is not supported",
    "DDDD": "day of year is not supported",
    "M": "use MM",
    "H": "use HH:mm (24-hour)",
    "h": "use HH:mm (24-hour)",
    "hh": "use HH:mm (24-hour)",
    "A": "use HH:mm (24-hour)",
    "a": "use HH:mm (24-hour)",
    "ss": "seconds are not supported",
    "date": "use {{YYYY-MM-DD}}",
    "time": "use {{HH:mm}}",
}


class NameTemplateError(ValueError):
    """An automation name contains an invalid date placeholder."""


def _can_segment_tokens(run: str) -> bool:
    reachable = [False] * (len(run) + 1)
    reachable[0] = True
    for position in range(len(run)):
        if not reachable[position]:
            continue
        for token in _TOKENS:
            if run.startswith(token, position):
                reachable[position + len(token)] = True
    return reachable[-1]


def _pattern(body: str) -> tuple[str, ...]:
    if "%" in body:
        raise NameTemplateError("strftime codes are not supported; " + _SUPPORTED)
    body = body.strip(" \t")
    runs = tuple(_RUNS.findall(body))
    if not runs or "".join(runs) != body:
        raise NameTemplateError(_SUPPORTED)
    for run in runs[::2]:
        if run not in _TOKENS:
            hint = _HINTS.get(run)
            if hint is None and _can_segment_tokens(run):
                hint = (
                    "separate the tokens or use adjacent placeholders, e.g. {{YYYY}}{{MM}}{{DD}}"
                )
            raise NameTemplateError(hint or _SUPPORTED)
    if len(runs) % 2 == 0:
        raise NameTemplateError("patterns must start and end with a token; " + _SUPPORTED)
    if "mm" in runs and "HH" not in runs:
        raise NameTemplateError(_CLOCK_HINT)
    for index in range(0, len(runs) - 2, 2):
        if runs[index] == "HH" and ":" in runs[index + 1] and runs[index + 2] == "MM":
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
            body = name[index + 2 : closing] if closing != -1 else ""
            if closing == -1 or any(character in body for character in "{}\r\n"):
                raise NameTemplateError("unclosed or nested placeholder; " + _SUPPORTED)
            parts.append(_pattern(body))
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
    rendered = "".join(
        part if isinstance(part, str) else "".join(values.get(run, run) for run in part)
        for part in parts
    )
    if len(rendered) > 768:
        raise NameTemplateError("rendered names must be at most 768 Unicode code points")
    return rendered
