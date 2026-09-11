import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(".github/workflows/ui-preview.yml").read_text()
DEPLOY_JOB = yaml.safe_load(WORKFLOW)["jobs"]["deploy"]
PROVISION_STEP = next(step for step in DEPLOY_JOB["steps"] if step.get("id") == "app")
APP_NAME = "omnigent-ui-preview-pr-123"
APP_DESCRIPTION = "https://github.com/omnigent-ai/omnigent/pull/123"
APP_CREATOR = "preview-deployer"
APP_URL = "https://preview.example.com"

Reply = tuple[str, object, int]


def _app(state: str = "STARTING", **fields: object) -> dict[str, object]:
    return {
        "id": "app-1",
        "name": APP_NAME,
        "description": APP_DESCRIPTION,
        "creator": APP_CREATOR,
        "compute_status": {"state": state, "message": "Provisioning app dependencies"},
        "url": APP_URL,
        **fields,
    }


def _reply(command: str, output: object = None, code: int = 0) -> Reply:
    return command, output, code


def _run_provision(
    tmp_path: Path, replies: list[Reply]
) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
    if not shutil.which("bash") or not shutil.which("jq"):
        pytest.skip("Workflow execution requires bash and jq")

    for index, (command, output, code) in enumerate(replies):
        (tmp_path / f"{index}.command").write_text(command)
        (tmp_path / f"{index}.output").write_text(
            output if isinstance(output, str) else json.dumps(output)
        )
        (tmp_path / f"{index}.code").write_text(str(code))
    (tmp_path / "index").write_text("0")

    cli = tmp_path / "databricks"
    cli.write_text(
        r"""#!/usr/bin/env bash
set -euo pipefail
index=$(<"$PREVIEW_TEST_DIR/index")
printf '%s\n' "$((index + 1))" > "$PREVIEW_TEST_DIR/index"
printf '%s\n' "$*" >> "$PREVIEW_TEST_DIR/commands"
if [[ ! -f "$PREVIEW_TEST_DIR/$index.command" ]]; then
  echo "Unexpected Databricks call: $*" >&2
  exit 99
fi
expected=$(<"$PREVIEW_TEST_DIR/$index.command")
if [[ "$1 $2" != "apps $expected" ]]; then
  echo "Expected apps $expected, got $*" >&2
  exit 99
fi
code=$(<"$PREVIEW_TEST_DIR/$index.code")
if [[ "$code" == 0 ]]; then
  printf '%s\n' "$(<"$PREVIEW_TEST_DIR/$index.output")"
else
  printf '%s\n' "$(<"$PREVIEW_TEST_DIR/$index.output")" >&2
fi
exit "$code"
"""
    )
    cli.chmod(0o755)
    sleep = tmp_path / "sleep"
    sleep.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$PREVIEW_TEST_DIR/sleeps"\n')
    sleep.chmod(0o755)

    env = {key: value for key, value in os.environ.items() if not key.startswith("DATABRICKS_")}
    env.pop("BASH_ENV", None)
    env.update(
        {
            "PATH": f"{tmp_path}{os.pathsep}{env['PATH']}",
            "PREVIEW_TEST_DIR": str(tmp_path),
            "APP_NAME": APP_NAME,
            "APP_DESCRIPTION": APP_DESCRIPTION,
            "DATABRICKS_CLIENT_ID": APP_CREATOR,
            "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        }
    )
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", PROVISION_STEP["run"]],
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    commands = (tmp_path / "commands").read_text().splitlines()
    output_path = tmp_path / "github-output"
    output = output_path.read_text() if output_path.exists() else ""
    return result, commands, output


def test_provision_new_app(tmp_path: Path) -> None:
    result, commands, output = _run_provision(
        tmp_path, [_reply("list", []), _reply("create", _app()), _reply("get", _app("ACTIVE"))]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output == f"url={APP_URL}\n"
    create = next(command for command in commands if command.startswith("apps create "))
    payload = json.loads(create.split("--json ", 1)[1].split(" --no-wait", 1)[0])
    assert payload == {"name": APP_NAME, "description": APP_DESCRIPTION}
    assert not any(command.startswith("apps delete") for command in commands)


def test_provision_recreates_timed_out_app_after_deletion_finishes(tmp_path: Path) -> None:
    result, commands, output = _run_provision(
        tmp_path,
        [
            _reply("list", []),
            _reply("create", _app()),
            *[_reply("get", _app())] * 41,
            _reply("delete"),
            _reply("list", [_app()]),
            _reply("list", []),
            _reply("create", _app(id="app-2")),
            _reply("get", _app("ACTIVE", id="app-2", url="https://retry.example.com")),
        ],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output == "url=https://retry.example.com\n"
    assert "Provisioning app dependencies" in result.stdout
    assert "Provisioning attempt 2/3" in result.stdout
    assert f"apps delete {APP_NAME} --auto-approve" in commands
    assert sum(command.startswith("apps create") for command in commands) == 2
    assert (tmp_path / "sleeps").read_text().splitlines() == ["15"] * 40 + ["3", "15"]


def test_provision_stops_after_three_attempts(tmp_path: Path) -> None:
    replies = [_reply("list", [])]
    for attempt in range(3):
        app = _app(id=f"app-{attempt}")
        replies.extend([_reply("create", app), *[_reply("get", app)] * 41])
        if attempt < 2:
            replies.extend([_reply("delete"), _reply("list", [])])
    result, commands, output = _run_provision(tmp_path, replies)
    assert result.returncode != 0
    assert "after 3 attempts" in result.stdout
    assert sum(command.startswith("apps create") for command in commands) == 3
    assert sum(command.startswith("apps delete") for command in commands) == 2
    assert output == ""


def test_provision_recovers_undeployed_app_from_previous_run(tmp_path: Path) -> None:
    result, commands, output = _run_provision(
        tmp_path,
        [
            _reply("list", [_app()]),
            _reply("get", _app()),
            *[_reply("get", _app())] * 41,
            _reply("delete"),
            _reply("list", []),
            _reply("create", _app(id="app-2")),
            _reply("get", _app("ACTIVE", id="app-2")),
        ],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output == f"url={APP_URL}\n"
    assert sum(command.startswith("apps create") for command in commands) == 1


@pytest.mark.parametrize("state", ["ACTIVE", "STARTING", "STOPPED"])
def test_provision_preserves_existing_deployed_app(tmp_path: Path, state: str) -> None:
    app = _app(state, active_deployment={"deployment_id": "deployed"})
    result, commands, output = _run_provision(
        tmp_path, [_reply("list", [app]), _reply("get", app)]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output == f"url={APP_URL}\n"
    assert commands == ["apps list -o json", f"apps get {APP_NAME} -o json"]


@pytest.mark.parametrize("state", ["ERROR", "STOPPED"])
def test_provision_retries_terminal_compute_states(tmp_path: Path, state: str) -> None:
    result, commands, _ = _run_provision(
        tmp_path,
        [
            _reply("list", []),
            _reply("create", _app()),
            _reply("get", _app(state)),
            _reply("get", _app(state)),
            _reply("delete"),
            _reply("list", []),
            _reply("create", _app(id="app-2")),
            _reply("get", _app("ACTIVE", id="app-2")),
        ],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert sum(command.startswith("apps create") for command in commands) == 2


def test_provision_rechecks_readiness_before_deleting(tmp_path: Path) -> None:
    result, commands, output = _run_provision(
        tmp_path,
        [
            _reply("list", []),
            _reply("create", _app()),
            *[_reply("get", _app())] * 40,
            _reply("get", _app("ACTIVE")),
        ],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output == f"url={APP_URL}\n"
    assert not any(command.startswith("apps delete") for command in commands)


@pytest.mark.parametrize(
    "changed_fields",
    [
        {"creator": "another-principal"},
        {"description": "another-app"},
        {"resources": [{"name": "database"}]},
        {"active_deployment": {"deployment_id": "deployed"}},
        {"pending_deployment": {"deployment_id": "deploying"}},
        {"pending_update": {"update_id": "updating"}},
        {"default_source_code_path": "/Workspace/another-app"},
    ],
)
def test_provision_refuses_to_delete_configured_or_foreign_app(
    tmp_path: Path, changed_fields: dict[str, object]
) -> None:
    result, commands, output = _run_provision(
        tmp_path,
        [
            _reply("list", []),
            _reply("create", _app()),
            _reply("get", _app("ERROR")),
            _reply("get", _app("ERROR", **changed_fields)),
        ],
    )
    assert result.returncode != 0
    assert "refusing to recreate" in result.stdout
    assert not any(command.startswith("apps delete") for command in commands)
    assert output == ""


@pytest.mark.parametrize("replacement_timing", ["provisioning", "deletion"])
def test_provision_aborts_if_app_identity_changes(tmp_path: Path, replacement_timing: str) -> None:
    replies = [_reply("list", []), _reply("create", _app())]
    if replacement_timing == "provisioning":
        replies.append(_reply("get", _app("ACTIVE", id="replacement")))
    else:
        replies.extend(
            [
                _reply("get", _app("ERROR")),
                _reply("get", _app("ERROR")),
                _reply("delete"),
                _reply("list", [_app(id="replacement")]),
            ]
        )
    result, commands, output = _run_provision(tmp_path, replies)
    assert result.returncode != 0
    assert "App identity changed" in result.stdout
    assert sum(command.startswith("apps create") for command in commands) == 1
    assert output == ""


def test_provision_does_not_create_until_deletion_is_confirmed(tmp_path: Path) -> None:
    result, commands, output = _run_provision(
        tmp_path,
        [
            _reply("list", []),
            _reply("create", _app()),
            _reply("get", _app("ERROR")),
            _reply("get", _app("ERROR")),
            _reply("delete"),
            *[_reply("list", [_app()])] * 20,
        ],
    )
    assert result.returncode != 0
    assert "App still exists after deletion" in result.stdout
    assert sum(command.startswith("apps create") for command in commands) == 1
    assert output == ""


@pytest.mark.parametrize("failing_call", range(6))
def test_provision_does_not_retry_cli_errors(tmp_path: Path, failing_call: int) -> None:
    replies = [
        _reply("list", []),
        _reply("create", _app()),
        _reply("get", _app("ERROR")),
        _reply("get", _app("ERROR")),
        _reply("delete"),
        _reply("list", []),
    ][: failing_call + 1]
    replies[-1] = _reply(replies[-1][0], "Permission denied", code=1)
    result, commands, output = _run_provision(tmp_path, replies)
    assert result.returncode != 0
    assert "Permission denied" in result.stderr
    assert len(commands) == failing_call + 1
    assert output == ""


def test_provision_timeout_fits_within_deploy_job() -> None:
    assert PROVISION_STEP["timeout-minutes"] == 40
    assert DEPLOY_JOB["timeout-minutes"] == 60


@pytest.mark.parametrize("url", [None, "", "Unavailable"])
def test_provision_rejects_unavailable_url(tmp_path: Path, url: str | None) -> None:
    result, commands, output = _run_provision(
        tmp_path,
        [
            _reply("list", []),
            _reply("create", _app()),
            _reply("get", _app("ACTIVE", url=url)),
        ],
    )
    assert result.returncode != 0
    assert "App has no usable preview URL" in result.stdout
    assert not any(command.startswith("apps delete") for command in commands)
    assert output == ""


def test_cleanup_worker_requires_reconciler_identity_fields() -> None:
    for field in (
        "expected_id",
        "expected_create_time",
        "expected_update_time",
        "expected_creator",
    ):
        assert f"{field}:" in WORKFLOW
        assert f"inputs.{field}" in WORKFLOW


def test_cleanup_worker_revalidates_and_verifies_deletion() -> None:
    assert "App changed after cleanup review; refusing deletion" in WORKFLOW
    assert ".id == $id" in WORKFLOW
    assert ".create_time == $create_time" in WORKFLOW
    assert ".update_time == $update_time" in WORKFLOW
    assert "App still exists after deletion" in WORKFLOW


def test_dispatch_cleanup_preserves_workspace_source() -> None:
    assert 'if [[ "$DISPATCHED" != true && -n "$SOURCE_PATH" ]]' in WORKFLOW


def test_dispatch_cleanup_reports_foreign_creator_without_claiming_deletion() -> None:
    assert 'if [[ "$EXPECTED_CREATOR" != "$DATABRICKS_CLIENT_ID" ]]' in WORKFLOW
    assert "record_result retain 'app belongs to another deployment principal'" in WORKFLOW
    assert "name: ui-preview-cleanup-result" in WORKFLOW
    assert "if: steps.delete.outputs.action == 'deleted' ||" in WORKFLOW


def test_dispatch_cleanup_has_a_separate_concurrency_group() -> None:
    assert "format('cleanup-{0}', inputs.pr_number)" in WORKFLOW
