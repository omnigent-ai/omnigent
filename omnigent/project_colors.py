"""Project colors: auto-assignment from the web UI's theme palette accents.

A project is marked by an emoji (``config["icon"]``) or a palette color
(``config["color"]``, a palette id). A project created with neither gets the
color most different from the colors the owner's other projects already use.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

from omnigent.project_colors_generated import PROJECT_COLOR_ACCENTS

_Lab = tuple[float, float, float]


def _srgb_channel_to_linear(channel: int) -> float:
    value = channel / 255
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _hex_to_oklab(hex_color: str) -> _Lab:
    """Convert ``#rrggbb`` to OKLab, a perceptually uniform space for distances."""
    red, green, blue = (
        _srgb_channel_to_linear(int(hex_color[index : index + 2], 16)) for index in (1, 3, 5)
    )
    long = math.cbrt(0.4122214708 * red + 0.5363288807 * green + 0.0514459929 * blue)
    medium = math.cbrt(0.2119034982 * red + 0.6806995457 * green + 0.1073969561 * blue)
    short = math.cbrt(0.0883024619 * red + 0.2817104257 * green + 0.6299152934 * blue)
    return (
        0.2104542553 * long + 0.793617785 * medium - 0.0040720468 * short,
        1.9779984951 * long - 2.428592205 * medium + 0.4505937099 * short,
        0.0259040371 * long + 0.7827717662 * medium - 0.808675766 * short,
    )


_OKLAB = {
    color_id: (_hex_to_oklab(light), _hex_to_oklab(dark))
    for color_id, (light, dark) in PROJECT_COLOR_ACCENTS.items()
}


def _distance(first: str, second: str) -> float:
    """Distance between two colors, summed over their light and dark accents."""
    return sum(math.dist(a, b) for a, b in zip(_OKLAB[first], _OKLAB[second], strict=True))


def pick_distinct_project_color(used_colors: Iterable[object]) -> str:
    """Pick the palette color most different from the colors already in use.

    Maximizes the distance to the nearest used color, so unused colors win
    before any repeats; ties go to the least-used color, then palette order.
    Values that are not palette ids are ignored.

    :param used_colors: Stored ``config["color"]`` values of existing projects.
    :returns: A palette id from :data:`PROJECT_COLOR_ACCENTS`.
    """
    used = [color for color in used_colors if color in PROJECT_COLOR_ACCENTS]
    candidates = list(PROJECT_COLOR_ACCENTS)
    if not used:
        return candidates[0]

    def score(candidate: str) -> tuple[float, int]:
        nearest = min(_distance(candidate, color) for color in set(used))
        return nearest, -used.count(candidate)

    # max() keeps the first of equal scores, i.e. palette order.
    return max(candidates, key=score)


def with_auto_project_color(
    config: Mapping[str, Any], existing_configs: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """Return ``config`` with a color assigned when it has no emoji or color.

    :param config: The new project's requested config.
    :param existing_configs: Configs of the owner's other projects.
    :returns: A copy of ``config``, with ``color`` added only when needed.
    """
    if config.get("icon") or config.get("color") in PROJECT_COLOR_ACCENTS:
        return dict(config)
    used = [existing.get("color") for existing in existing_configs]
    return {**config, "color": pick_distinct_project_color(used)}
