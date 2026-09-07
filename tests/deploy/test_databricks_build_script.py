from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    (
        "skip_web_ui",
        "enable_canvas",
        "expected_pnpm_calls",
        "expected_uv_calls",
        "expected_build_hook_setting",
    ),
    [
        (False, False, 2, 3, "<unset>"),
        (False, True, 2, 4, "<unset>"),
        (True, True, 0, 3, "true"),
    ],
)
def test_build_script_propagates_api_only_mode_to_wheel_builds(
    tmp_path: Path,
    *,
    skip_web_ui: bool,
    enable_canvas: bool,
    expected_pnpm_calls: int,
    expected_uv_calls: int,
    expected_build_hook_setting: str,
) -> None:
    repo = tmp_path / "repo"
    script = repo / "deploy" / "databricks" / "build.sh"
    script.parent.mkdir(parents=True)
    shutil.copyfile(
        Path(__file__).parents[2] / "deploy" / "databricks" / "build.sh",
        script,
    )

    command_log = tmp_path / "commands.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "pnpm",
        '#!/usr/bin/env bash\nprintf "pnpm|%s\\n" "$*" >> "$COMMAND_LOG"\n',
    )
    _write_executable(
        fake_bin / "uv",
        "#!/usr/bin/env bash\n"
        'printf "uv|%s|%s\\n" "${OMNIGENT_SKIP_WEB_UI-<unset>}" "$*" '
        '>> "$COMMAND_LOG"\n'
        "mkdir -p dist\n"
        "touch dist/fake.whl\n",
    )

    env = os.environ.copy()
    env.update(
        {
            "COMMAND_LOG": str(command_log),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        }
    )
    env.pop("OMNIGENT_SKIP_WEB_UI", None)
    if skip_web_ui:
        env["SKIP_WEB_UI"] = "1"
    else:
        env.pop("SKIP_WEB_UI", None)
    if enable_canvas:
        env["OMNIGENT_ENABLE_CANVAS"] = "true"
    else:
        env.pop("OMNIGENT_ENABLE_CANVAS", None)

    subprocess.run(["bash", str(script)], cwd=repo, env=env, check=True)

    commands = command_log.read_text().splitlines()
    assert sum(command.startswith("pnpm|") for command in commands) == expected_pnpm_calls
    uv_commands = [command for command in commands if command.startswith("uv|")]
    assert len(uv_commands) == expected_uv_calls
    assert all(command.split("|", 2)[1] == expected_build_hook_setting for command in uv_commands)
    assert any("extensions/canvas/" in command for command in uv_commands) is (
        enable_canvas and not skip_web_ui
    )


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)
