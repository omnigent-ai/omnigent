"""Exercise Polly's workflow cleanup and GitHub output handoff in a real shell."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.posix_only

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/polly-review.yml"
_MARKER = "<!-- POLLY_REVIEW_START -->"
_REVIEW = "## Blocking issues\nNone.\n## Summary\nDone.\n"
_QUOTED_REVIEW = f"## Blocking issues\nDo not strip inline `{_MARKER}` text.\n"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(f"{_MARKER}\n{_REVIEW}", _REVIEW, id="single-review"),
        pytest.param(
            f"{_MARKER}\n## Blocking issues\nPartial draft\n{_MARKER}\n{_REVIEW}",
            _REVIEW,
            id="draft-then-final",
        ),
        pytest.param(f"{_MARKER}\n{_QUOTED_REVIEW}", _QUOTED_REVIEW, id="inline-quote"),
        pytest.param(
            f"{_MARKER}\nPartial draft\n{_MARKER}\n{_QUOTED_REVIEW}",
            _QUOTED_REVIEW,
            id="draft-then-inline-quote",
        ),
        pytest.param(f"Starting review.\n{_REVIEW}", _REVIEW, id="heading-fallback"),
        pytest.param(_QUOTED_REVIEW, _QUOTED_REVIEW, id="heading-with-inline-quote"),
        pytest.param(f"Will emit `{_MARKER}` later.\n", "", id="narration-with-inline-quote"),
        pytest.param("Waiting for results.\n", "", id="narration-only"),
        pytest.param("", "", id="empty"),
        pytest.param(" \n\t", "", id="whitespace-only"),
        pytest.param(f"{_MARKER}\n{_REVIEW}{_MARKER}\n", "", id="empty-final-review"),
        pytest.param(f"{_MARKER}\n \t\n", "", id="whitespace-final-review"),
        pytest.param(_MARKER, "", id="marker-at-eof"),
    ],
)
def test_review_output_preserves_final_review(tmp_path: Path, raw: str, expected: str) -> None:
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    step = next(s for s in workflow["jobs"]["review"]["steps"] if s.get("id") == "polly")
    # Replay the workflow from captured CLI stdout through GITHUB_OUTPUT.
    script = step["run"][step["run"].index('python3 -c "') :]
    output = tmp_path / "polly_output.txt"
    review = tmp_path / "polly_review.txt"
    output.write_text(raw)
    script = script.replace("/tmp/polly_output.txt", str(output))
    script = script.replace("/tmp/polly_review.txt", str(review))
    github_output = tmp_path / "github_output"
    (tmp_path / "python3").symlink_to(sys.executable)
    env = {
        "PATH": f"{tmp_path}{os.pathsep}{os.defpath}",
        "GITHUB_OUTPUT": str(github_output),
    }
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert output.read_text() == raw
    if not expected:
        assert result.returncode != 0
        assert "Polly produced no publishable review" in result.stderr
        assert not github_output.exists()
        assert not review.exists()
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert review.read_text() == expected
    header, payload = github_output.read_text().split("\n", 1)
    assert header.startswith("review_text<<")
    delimiter = header.removeprefix("review_text<<")
    assert payload == f"{expected}{delimiter}\n"


def test_failure_diagnostics_preserves_logs_without_gateway_secrets(tmp_path: Path) -> None:
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    step = next(
        s
        for s in workflow["jobs"]["review"]["steps"]
        if s.get("name") == "Prepare Polly failure diagnostics"
    )
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "runner.log").write_text("request failed: test-api-secret at https://gateway.test")
    (logs / "config.yaml").write_text("not a process log")
    (tmp_path / "polly-stderr.log").write_text("stderr: test-api-secret")
    output = tmp_path / "polly_output.txt"
    output.write_text("Waiting for results. https://gateway.test")
    destination = tmp_path / "diagnostics"
    script = step["run"].replace("/tmp/polly_output.txt", str(output))
    script = script.replace("/tmp/polly_review.txt", str(tmp_path / "polly_review.txt"))
    script = script.replace("/tmp/polly-diagnostics", str(destination))
    script = script.replace(
        "pathlib.Path.home() / '.omnigent' / 'logs'", f"pathlib.Path({str(logs)!r})"
    )
    (tmp_path / "python3").symlink_to(sys.executable)
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=tmp_path,
        env={
            "PATH": f"{tmp_path}{os.pathsep}{os.defpath}",
            "LLM_API_KEY": "test-api-secret",
            "GATEWAY_BASE_URL": "https://gateway.test",
        },
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (destination / "polly-stderr.log").read_text() == "stderr: [REDACTED]"
    assert (destination / "polly-output.txt").read_text() == "Waiting for results. [REDACTED]"
    assert (destination / "process-logs/runner.log").read_text() == (
        "request failed: [REDACTED] at [REDACTED]"
    )
    assert not (destination / "process-logs/config.yaml").exists()
    assert not (destination / "polly-review.txt").exists()


@pytest.mark.parametrize("failure_step", ["secret-scan", "posting"])
def test_later_failure_retains_raw_stdout(tmp_path: Path, failure_step: str) -> None:
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    steps = {step["name"]: step for step in workflow["jobs"]["review"]["steps"]}
    review = _REVIEW + ("test-api-secret\n" if failure_step == "secret-scan" else "")
    raw = f"Starting review: test-api-secret at https://gateway.test\n{_MARKER}\n{review}"
    (tmp_path / "stdout.txt").write_text(raw)
    (tmp_path / "review_prompt.txt").write_text("Synthetic review; no model calls.")
    (tmp_path / "python3").symlink_to(sys.executable)
    commands = {
        "uv": 'cat "$POLLY_TEST_STDOUT"\necho "stderr: test-api-secret" >&2\n',
        "gh": 'echo "Forced posting failure" >&2\nexit 42\n',
    }
    for name, command in commands.items():
        executable = tmp_path / name
        executable.write_text("#!/bin/sh\n" + command)
        executable.chmod(0o755)
    env = {
        "PATH": f"{tmp_path}{os.pathsep}{os.defpath}",
        "POLLY_TEST_STDOUT": str(tmp_path / "stdout.txt"),
        "GITHUB_OUTPUT": str(tmp_path / "github_output"),
        "OMNIGENT_POLLY_HARNESS": "unused",
        "OMNIGENT_POLLY_MODEL": "unused",
        "LLM_API_KEY": "test-api-secret",
        "GATEWAY_BASE_URL": "https://gateway.test",
        "PR_NUMBER": "1",
        "REPO": "test/repo",
        "HEAD_SHA": "test-sha",
        "RUN_URL": "https://example.test/run",
    }

    def run_step(name: str) -> subprocess.CompletedProcess[str]:
        script = steps[name]["run"].replace("/tmp/", str(tmp_path) + "/")
        script = script.replace(
            "pathlib.Path.home() / '.omnigent' / 'logs'",
            f"pathlib.Path({str(tmp_path / 'logs')!r})",
        )
        return subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", script],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    result = run_step("Run Polly review")
    assert result.returncode == 0, result.stdout + result.stderr
    header, payload = (tmp_path / "github_output").read_text().split("\n", 1)
    delimiter = header.removeprefix("review_text<<")
    assert payload == f"{review}{delimiter}\n"
    env["REVIEW_TEXT"] = payload.removesuffix(f"{delimiter}\n")
    result = run_step("Scan review output for secrets before posting")
    if failure_step == "secret-scan":
        assert result.returncode != 0
        assert "Review output contains LLM_API_KEY" in result.stdout
        assert not (tmp_path / "comment.md").exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        result = run_step("Post review comment")
        assert result.returncode == 42
        assert "Forced posting failure" in result.stderr
        assert review in (tmp_path / "comment.md").read_text()
        assert "Starting review" not in (tmp_path / "comment.md").read_text()
    result = run_step("Prepare Polly failure diagnostics")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "polly_output.txt").read_text() == raw
    assert (tmp_path / "polly_review.txt").read_text() == review
    diagnostic_dir = tmp_path / "polly-diagnostics"
    assert (diagnostic_dir / "polly-output.txt").read_text() == raw.replace(
        "test-api-secret", "[REDACTED]"
    ).replace("https://gateway.test", "[REDACTED]")
    assert (diagnostic_dir / "polly-review.txt").read_text() == review.replace(
        "test-api-secret", "[REDACTED]"
    )
    assert (diagnostic_dir / "polly-stderr.log").read_text() == "stderr: [REDACTED]\n"
