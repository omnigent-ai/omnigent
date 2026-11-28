"""Source scope, shell URLs, suppressions, and tool integration for visibility lint."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from dev.lint import lint_session_list_visibility as lint


@pytest.mark.parametrize(
    "path",
    [
        "omnigent/cli.py",
        "omnigent/server/client.py",
        "sdks/python-client/client.py",
        "web/src/hooks/sessions.ts",
        "web/electron/e2e/scenario.mjs",
        "editors/vscode/client.ts",
        "scripts/backend-smoke.sh",
        "dev/benchmarks/omnigent/journeys.py",
        ".claude/skills/polly-e2e-dev/polly_cuj.py",
        "tests/e2e/test_sessions.py",
        "tests/e2e_ui/conftest.py",
        "tests/integration/test_sharing.py",
        "tests/_helpers/live_server.py",
        "tests/harness_bench/full_server.py",
    ],
)
def test_client_sources_are_checked(path: str) -> None:
    assert lint.is_scannable_path(Path(path))


@pytest.mark.parametrize(
    "path",
    [
        "tests/server/test_sessions.py",
        "tests/frontends/sdk/test_sessions_namespace.py",
        "tests/repl/test_resume_picker.py",
        "integrations/slack/tests/fakes.py",
        "web/src/client.test.ts",
        "web/src/client.spec.tsx",
        "web/src/__tests__/client.ts",
        "assistant-ui/client.py",
        "omnigent/server/static/web-ui/client.js",
        "examples/extensions/hello-page/dist/client.js",
        "dev/lint/session_list_visibility.mjs",
    ],
)
def test_fixtures_and_generated_sources_are_excluded(path: str) -> None:
    assert not lint.is_scannable_path(Path(path))


@pytest.mark.parametrize(
    "source",
    [
        'curl "$SERVER/v1/sessions"',
        "for path in /health /v1/sessions; do",
        'curl --get --data-urlencode "limit=20" "$SERVER/v1/sessions"',
        'curl "https://example.test/v1/sessions?visibility="',
        'curl -X POST "$SERVER/v1/sessions"; curl "$SERVER/v1/sessions"',
        'curl "$SERVER/v1/sessions" & curl -XPOST "$SERVER/v1/sessions"',
        'curl -G -H "visibility=all" "$SERVER/v1/sessions"',
        'curl -X GET --data "visibility=all" "$SERVER/v1/sessions"',
    ],
)
def test_shell_list_urls_need_visibility(source: str) -> None:
    assert lint._scan_shell(source) == [(1, "session-list URL must specify visibility")]


@pytest.mark.parametrize(
    "source",
    [
        'curl "$SERVER/v1/sessions?visibility=all"',
        'for path in /health "/v1/sessions?visibility=all"; do',
        'curl --get --data-urlencode "visibility=mine" "$SERVER/v1/sessions"',
        'curl -G -dvisibility=all "$SERVER/v1/sessions"',
        'curl --get --data-urlencode=visibility=mine "$SERVER/v1/sessions"',
        'curl -X POST "$SERVER/v1/sessions"',
        'curl -XPOST "$SERVER/v1/sessions"',
        'curl --request=POST "$SERVER/v1/sessions"',
        'curl --data "{}" "$SERVER/v1/sessions"',
        'curl -dvisibility=all "$SERVER/v1/sessions"',
        'curl "$SERVER/v1/sessions/conv_a"',
        'curl "$SERVER/v1/sessions/projects"',
        '# curl "$SERVER/v1/sessions"',
        'echo "$SERVER/v1/sessions"',
    ],
)
def test_shell_explicit_visibility_and_other_endpoints_pass(source: str) -> None:
    assert lint._scan_shell(source) == []


def test_python_suppression_is_on_the_request_line(tmp_path: Path) -> None:
    path = tmp_path / "client.py"
    path.write_text(
        "# custom-lint: disable-next=session-list-visibility -- external query builder\n"
        'client.get("/v1/sessions", params=external_params())\n'
        'client.get("/v1/sessions")\n'
    )
    messages = lint.check_paths([path])
    assert len(messages) == 1
    assert messages[0].startswith(f"{path}:3:")


def test_comment_marker_in_a_string_does_not_suppress(tmp_path: Path) -> None:
    path = tmp_path / "client.py"
    path.write_text(
        'client.get("/v1/sessions", headers={"x-note": '
        '"# custom-lint: disable=session-list-visibility"})\n'
    )
    assert len(lint.check_paths([path])) == 1


def test_typescript_results_keep_file_and_line(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, '[{"path":"web/client.ts","line":7,"message":"missing visibility"}]', ""
        )

    monkeypatch.setattr(lint.subprocess, "run", run)
    assert lint._check_typescript([Path("web/client.ts")]) == [
        "web/client.ts:7: missing visibility"
    ]
    assert calls[0][-1] == "web/client.ts"


@pytest.mark.parametrize("mode", ["missing-node", "missing-typescript", "bad-json"])
def test_typescript_tool_failures_do_not_pass(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if mode == "missing-node":
            raise FileNotFoundError("node")
        if mode == "missing-typescript":
            return subprocess.CompletedProcess(args, 1, "", "Cannot find typescript")
        return subprocess.CompletedProcess(args, 0, "{}", "")

    monkeypatch.setattr(lint.subprocess, "run", run)
    assert lint._check_typescript([Path("web/client.ts")])


def test_precommit_trigger_covers_sources_and_lint_config() -> None:
    config = yaml.safe_load(Path(".pre-commit-config.yaml").read_text())
    hook = next(
        hook for repo in config["repos"] for hook in repo["hooks"] if hook["id"] == "custom-lint"
    )
    pattern = re.compile(hook["files"])
    assert not hook["pass_filenames"]
    for path in lint._iter_scannable_paths():
        assert pattern.search(path.as_posix()), path
    for path in [".pre-commit-config.yaml", ".github/workflows/lint.yml"]:
        assert pattern.search(path)
    for extension in lint.SOURCE_EXTENSIONS:
        assert pattern.search(f"new_client{extension}")
