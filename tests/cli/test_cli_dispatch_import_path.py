"""CLI dispatch must not import workspace modules in place of installed runtime dependencies.

No LLM or server is needed: pytest tests/cli/test_cli_dispatch_import_path.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLI_TIMEOUT_S = 120.0

# `diagnose` imports this dependency only after dispatch has begun, which is
# when the launch directory has joined the import path.
_SHADOWED_DEPENDENCY = "anyio"

_ENV_TO_CLEAR = (
    "PYTHONSAFEPATH",
    "OMNIGENT_REQUIRE_WRAPPER",
    "OMNIGENT_WRAPPER_BYPASS",
    "DATABRICKS_TOKEN",
    "DATABRICKS_CONFIG_PROFILE",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OMNIGENT_AUTH_ENABLED",
    "OMNIGENT_OIDC_ISSUER",
    "OMNIGENT_AUTH_PROVIDER",
    "OMNIGENT_DATABASE_URI",
    "OMNIGENT_RUNNER_ID",
    "OMNIGENT_RUNNER_TUNNEL_TOKEN",
    "OMNIGENT_HOST_ID",
    "OMNIGENT_HOST_TOKEN",
)

_SHADOW_PACKAGE = """\
import json, os, sys

with open({marker!r}, "a") as fh:
    fh.write(json.dumps({{"pid": os.getpid(), "argv": list(sys.orig_argv)}}) + "\\n")

raise RuntimeError("workspace {name} package was imported by the omnigent CLI")
"""

_CONSOLE_SCRIPT = """\
import sys
from omnigent.cli import main

if __name__ == "__main__":
    sys.exit(main())
"""


def _isolated_state(root: Path) -> dict[str, str]:
    """Point every CLI state directory at *root*."""
    state = {
        "HOME": root / "home",
        "OMNIGENT_CONFIG_HOME": root / "config",
        "OMNIGENT_DATA_DIR": root / "data",
    }
    for path in state.values():
        path.mkdir(exist_ok=True)
    return {key: str(path) for key, path in state.items()}


def _isolated_env(root: Path, *extra_pythonpath: Path) -> dict[str, str]:
    """Isolate CLI state and credentials; PYTHONPATH is this checkout plus *extra_pythonpath*."""
    env = dict(os.environ)
    for key in _ENV_TO_CLEAR:
        env.pop(key, None)
    env.update(
        _isolated_state(root),
        PYTHONPATH=os.pathsep.join(str(path) for path in (_REPO_ROOT, *extra_pythonpath)),
    )
    return env


def _console_script(root: Path) -> Path:
    """Return the installed ``omnigent`` console script, or an equivalent shim."""
    installed = Path(sys.executable).with_name("omnigent")
    if installed.is_file():
        return installed
    shim = root / "bin" / "omnigent"
    shim.parent.mkdir(parents=True)
    shim.write_text(_CONSOLE_SCRIPT)
    return shim


def _launcher(entry: str, root: Path) -> list[str]:
    if entry == "console_script":
        return [sys.executable, str(_console_script(root))]
    if entry == "module":
        # The product spawns its own CLI children this way (see local_server.py).
        return [sys.executable, "-P", "-m", "omnigent.cli"]
    # A plain module run starts with the launch directory at sys.path[0].
    return [sys.executable, "-m", "omnigent.cli"]


def _run_diagnose(
    launcher: list[str], *, cwd: Path, env: dict[str, str], importtime: bool = False
) -> subprocess.CompletedProcess[str]:
    interpreter, *rest = launcher
    flags = ["-X", "importtime"] if importtime else []
    return subprocess.run(
        [interpreter, *flags, *rest, "diagnose"],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=_CLI_TIMEOUT_S,
    )


def _imported_modules(importtime_stderr: str) -> set[str]:
    return {
        line.rsplit("|", 1)[1].strip()
        for line in importtime_stderr.splitlines()
        if line.startswith("import time:")
    }


def _read_marker(marker: Path) -> list[dict[str, object]]:
    try:
        lines = marker.read_text().splitlines()
    except OSError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


@pytest.mark.parametrize(
    "entry",
    [
        "console_script",
        "module",
        "module_without_safe_path",
        "module_with_workspace_on_pythonpath",
    ],
)
def test_cli_dispatch_ignores_workspace_shadow_of_runtime_dependency(
    tmp_path: Path, entry: str
) -> None:
    """``omnigent diagnose`` from a workspace behaves exactly as from an empty directory."""
    (tmp_path / "clean").mkdir()
    workspace = tmp_path / "workspace"
    marker = tmp_path / "workspace-imports.jsonl"
    package = workspace / _SHADOWED_DEPENDENCY
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        _SHADOW_PACKAGE.format(marker=str(marker), name=_SHADOWED_DEPENDENCY)
    )
    env = _isolated_env(tmp_path)
    launcher = _launcher(entry, tmp_path)

    control = _run_diagnose(launcher, cwd=tmp_path / "clean", env=env, importtime=True)
    assert control.returncode == 0, (
        f"control run failed (rc={control.returncode}).\n{control.stdout}\n{control.stderr}"
    )
    assert _SHADOWED_DEPENDENCY in _imported_modules(control.stderr), (
        f"`diagnose` no longer imports {_SHADOWED_DEPENDENCY!r} after dispatch; "
        "pick another dependency it imports lazily so this test stays meaningful."
    )
    if entry == "module_with_workspace_on_pythonpath":
        # PYTHONPATH names the launch directory too, so it starts on sys.path twice.
        env = _isolated_env(tmp_path, workspace)
    shadowed = _run_diagnose(launcher, cwd=workspace, env=env)
    workspace_imports = _read_marker(marker)
    assert (
        not workspace_imports and shadowed.returncode == 0 and shadowed.stdout == control.stdout
    ), (
        f"`omnigent diagnose` run from a workspace holding a top-level "
        f"{_SHADOWED_DEPENDENCY!r} package diverged from the clean-directory run "
        f"(rc={shadowed.returncode}).\n"
        f"--- workspace modules imported by the CLI ---\n{workspace_imports}\n"
        f"--- stdout (workspace) ---\n{shadowed.stdout}"
        f"--- stdout (clean) ---\n{control.stdout}"
        f"--- stderr (workspace) ---\n{shadowed.stderr}"
    )


def test_importing_the_cli_does_not_preload_the_shadowed_dependency(tmp_path: Path) -> None:
    """The regression relies on the dependency arriving after dispatch, not with the import."""
    probe = subprocess.run(
        [
            sys.executable,
            "-P",
            "-c",
            f"import sys, omnigent.cli; print({_SHADOWED_DEPENDENCY!r} in sys.modules)",
        ],
        cwd=tmp_path,
        env=_isolated_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=_CLI_TIMEOUT_S,
    )
    assert probe.returncode == 0 and probe.stdout.strip() == "False", (
        f"importing omnigent.cli already loads {_SHADOWED_DEPENDENCY!r}, so the workspace runs "
        f"would not exercise dispatch.\n{probe.stdout}\n{probe.stderr}"
    )


@pytest.mark.parametrize("already_present", [False, True])
def test_console_entry_puts_workspace_last(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, already_present: bool
) -> None:
    """``main()`` leaves a single launch-directory entry behind every other entry."""
    from omnigent.cli import main

    for key in _ENV_TO_CLEAR:
        monkeypatch.delenv(key, raising=False)
    for key, value in _isolated_state(tmp_path).items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)
    others = [entry for entry in sys.path if entry != str(tmp_path)]
    startup = [str(tmp_path), *others, str(tmp_path)] if already_present else list(others)
    monkeypatch.setattr(sys, "path", startup)
    monkeypatch.setattr(sys, "argv", ["omnigent", "--version"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
    assert sys.path == [*others, str(tmp_path)]
