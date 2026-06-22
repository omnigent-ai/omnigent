"""Tests for the no-global-asyncio-patch hook (``dev/lint/lint_no_global_asyncio_patch.py``).

Patching ``asyncio.<name>`` via a dotted-path string (or ``patch.object``
on a module's ``asyncio`` attribute) clobbers the process-wide singleton
and leaks the mock across pytest-xdist workers. The guard exempts paths
with 2+ segments after ``.asyncio.`` (e.g. ``websockets.asyncio.client``,
a subpackage, not the stdlib module).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dev.lint.lint_no_global_asyncio_patch import main, scan


@pytest.mark.parametrize(
    "line",
    [
        'patch("omnigent.tools.mcp.asyncio.sleep")',
        'mock.patch("omnigent.tools.mcp.asyncio.sleep", new_callable=AsyncMock)',
        'monkeypatch.setattr("omnigent.llms.client.asyncio.sleep", _fake)',
    ],
)
def test_scan_flags_global_asyncio_patch(tmp_path: Path, line: str) -> None:
    """Each banned patch shape that targets the asyncio singleton trips the hook."""
    f = tmp_path / "test_sample.py"
    f.write_text(f"{line}\n")
    assert scan(f) == [(1, line)], f"Expected {line!r} flagged on line 1."


@pytest.mark.parametrize(
    "line",
    [
        # Thin-helper indirection — the sanctioned alternative.
        'patch("omnigent.tools.mcp._sleep", new_callable=AsyncMock)',
        # 2+ segments after .asyncio. → a subpackage, not the stdlib module.
        'patch("websockets.asyncio.client.connect")',
    ],
)
def test_scan_ignores_helper_and_subpackage(tmp_path: Path, line: str) -> None:
    """The helper indirection and asyncio-subpackage paths must stay green."""
    f = tmp_path / "test_sample.py"
    f.write_text(f"{line}\n")
    assert scan(f) == [], f"{line!r} must not be flagged but was."


def test_main_exit_codes(tmp_path: Path) -> None:
    """``main`` returns 1 on a global asyncio patch, 0 on the helper form."""
    dirty = tmp_path / "test_dirty.py"
    dirty.write_text('patch("omnigent.x.asyncio.sleep")\n')
    clean = tmp_path / "test_clean.py"
    clean.write_text('patch("omnigent.x._sleep")\n')
    assert main(["lint_no_global_asyncio_patch.py", str(dirty)]) == 1
    assert main(["lint_no_global_asyncio_patch.py", str(clean)]) == 0
