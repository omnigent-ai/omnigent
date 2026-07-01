"""Tests for the ``canvas.enabled`` server-config flag parsing (#2).

Guards a subtle bug: the config YAML loader keeps scalars like ``false`` as
``str``, and ``bool("false")`` is ``True`` — so a naive ``bool()`` cast can
never turn Canvas off. :func:`_resolve_canvas_enabled` must coerce falsey
strings (and genuine bools) correctly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.cli import _load_config, _resolve_canvas_enabled


@pytest.mark.parametrize(
    ("yaml_text", "expected"),
    [
        pytest.param("", True, id="empty-config-defaults-on"),
        pytest.param("canvas:\n  enabled: false\n", False, id="bare-false"),
        pytest.param("canvas:\n  enabled: true\n", True, id="bare-true"),
        pytest.param('canvas:\n  enabled: "false"\n', False, id="quoted-false"),
        pytest.param("canvas:\n  enabled: no\n", False, id="bare-no"),
        pytest.param("canvas:\n  enabled: off\n", False, id="bare-off"),
        pytest.param("canvas: {}\n", True, id="empty-canvas-block-defaults-on"),
        pytest.param("other: 1\n", True, id="unrelated-config-defaults-on"),
    ],
)
def test_resolve_canvas_enabled_from_loaded_config(
    tmp_path: Path,
    yaml_text: str,
    expected: bool,
) -> None:
    """End-to-end: write YAML, load it the way ``server`` does, resolve the flag.

    Exercises the loader + coercion together so a regression where the loader
    yields a string and the coercion mis-handles it (the original ``bool()``
    bug) is caught.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml_text)
    cfg = _load_config(str(cfg_path))
    assert _resolve_canvas_enabled(cfg) is expected


def test_resolve_canvas_enabled_direct_dict_inputs() -> None:
    """The coercion accepts genuine bools and absent keys, not just strings."""
    assert _resolve_canvas_enabled({}) is True
    assert _resolve_canvas_enabled({"canvas": {"enabled": False}}) is False
    assert _resolve_canvas_enabled({"canvas": {"enabled": True}}) is True
    # A naive ``bool("false")`` would be True — the whole point of the helper.
    assert _resolve_canvas_enabled({"canvas": {"enabled": "false"}}) is False
    assert _resolve_canvas_enabled({"canvas": None}) is True
