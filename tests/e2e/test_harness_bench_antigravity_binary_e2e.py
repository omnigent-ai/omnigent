"""Harness-bench Antigravity binary-name e2e test.

Runs the real user command — ``python -m tests.harness_bench --harness
antigravity-native --dimension basic_turn --live`` — as a subprocess with a
controlled ``PATH`` and asserts the bench's CLI availability gate probes the
binary omnigent actually installs and launches (``agy``, see
``omnigent/antigravity_native_launch.py``), not a nonexistent ``antigravity``
binary derived by name-mangling the harness slug.

Without the mapping, a machine with Antigravity correctly installed
(``agy`` on PATH, no ``antigravity``) is skipped with the misleading reason
``'antigravity' CLI is not on PATH`` — sending the user to reinstall a CLI
they already have.

Both tests exercise only the skip path (no vendor TUI is launched), so they
are fast and need no credentials, gateway, or real Antigravity install.

Usage::

    python -m pytest tests/e2e/test_harness_bench_antigravity_binary_e2e.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The binary omnigent installs and drives for the Antigravity native harness
# (see agy_binary_path() in omnigent/antigravity_native_launch.py and the
# "agy" aliases in omnigent/harness_plugins.py).
_LAUNCHED_BINARY = "agy"

_BENCH_CMD = [
    sys.executable,
    "-m",
    "tests.harness_bench",
    "--harness",
    "antigravity-native",
    "--dimension",
    "basic_turn",
    "--live",
]


def _path_without(binaries: tuple[str, ...]) -> str:
    """Return the ambient PATH minus every dir that contains *binaries*.

    Keeps the environment realistic (python, coreutils, etc. still resolve)
    while guaranteeing neither the real nor a stub Antigravity CLI leaks into
    the subprocess's ``shutil.which`` lookups.
    """
    kept: list[str] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        if any((Path(entry) / name).exists() for name in binaries):
            continue
        kept.append(entry)
    return os.pathsep.join(kept)


def _run_bench(path_env: str) -> str:
    """Run the bench command with PATH=*path_env*; return stdout+stderr."""
    env = {**os.environ, "PATH": path_env}
    proc = subprocess.run(
        _BENCH_CMD,
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return proc.stdout + proc.stderr


def test_skip_reason_names_the_binary_omnigent_launches() -> None:
    """With no Antigravity CLI installed, the skip must name ``agy``.

    The skip itself is correct here (nothing is installed) — the assertion is
    that its reason points the user at the binary omnigent actually launches,
    not at an ``antigravity`` binary that no install ever provides.
    """
    output = _run_bench(_path_without((_LAUNCHED_BINARY, "antigravity")))

    assert "skipped" in output.lower(), output
    assert "'antigravity' CLI is not on PATH" not in output, (
        "bench probed a nonexistent 'antigravity' binary instead of the "
        f"'{_LAUNCHED_BINARY}' binary omnigent launches:\n{output}"
    )
    assert f"'{_LAUNCHED_BINARY}' CLI is not on PATH" in output, output


def test_bench_probes_the_installed_agy_binary(tmp_path: Path) -> None:
    """With ``agy`` present on PATH, the bench must acknowledge it.

    Reproduces the reported journey: a machine where ``command -v agy``
    resolves and ``command -v antigravity`` does not. The bench must probe the
    installed ``agy`` — it must never claim ``'antigravity' CLI is not on
    PATH``. The stub deliberately fails its ``--version`` probe so the run
    still ends in a bounded skip (a "broken install" reason that quotes the
    stub's path) instead of launching a live vendor TUI.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / _LAUNCHED_BINARY
    stub.write_text("#!/bin/sh\necho 'agy: stub install' >&2\nexit 3\n", encoding="utf-8")
    stub.chmod(0o755)

    path_env = str(bin_dir) + os.pathsep + _path_without((_LAUNCHED_BINARY, "antigravity"))
    output = _run_bench(path_env)

    assert "'antigravity' CLI is not on PATH" not in output, (
        f"Antigravity is installed as '{_LAUNCHED_BINARY}' at {stub}, but the "
        f"bench reported it missing under the wrong name:\n{output}"
    )
    # The probe reached the user's actual install: the skip reason (broken
    # stub) quotes the stub's path rather than inventing a missing binary.
    assert str(stub) in output, output
