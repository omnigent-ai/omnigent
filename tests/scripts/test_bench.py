"""Tests for ``scripts/bench.py``."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import time
from pathlib import Path

import pytest

from omnigent.llms._usage_observer import notify_from_dict

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SPEC = importlib.util.spec_from_file_location(
    "_bench_under_test", _REPO_ROOT / "scripts" / "bench.py"
)
assert _SPEC is not None and _SPEC.loader is not None
bench = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = bench
_SPEC.loader.exec_module(bench)


def _write_task(tmp_path: Path, command: str) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.txt").write_text("untouched\n", encoding="utf-8")
    task = tmp_path / "task.yaml"
    task.write_text(
        "\n".join(
            [
                "name: stub-task",
                "workspace: ./workspace",
                'prompt: "Create the marker file."',
                "success:",
                "  type: command",
                "  command: |",
                f"    {command}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return task


def _write_yaml(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_run_benchmark_captures_usage_and_renders_outputs(tmp_path: Path) -> None:
    task_path = _write_task(
        tmp_path,
        (
            'python -c "from pathlib import Path; '
            "raise SystemExit(0 if Path('marker.txt').exists() else 1)\""
        ),
    )
    usage_by_harness = {
        "claude-sdk": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        "codex": {"input_tokens": 23, "output_tokens": 5, "total_tokens": 28},
    }
    seen_workspaces: list[Path] = []

    def stub_runner(harness: str, workspace: Path, prompt: str) -> None:
        assert prompt == "Create the marker file."
        seen_workspaces.append(workspace)
        notify_from_dict(model="stub-model", usage=usage_by_harness[harness])
        (workspace / "marker.txt").write_text(f"{harness}\n", encoding="utf-8")

    task = bench.load_task(task_path)
    comparison = bench.run_benchmark(task, ["claude-sdk", "codex"], runner=stub_runner)

    assert [result.harness for result in comparison.results] == ["claude-sdk", "codex"]
    for result in comparison.results:
        assert result.success is True
        assert result.error is None
        assert result.wall_time_s >= 0
        assert isinstance(result.wall_time_s, float)
        expected = usage_by_harness[result.harness]
        assert result.input_tokens == expected["input_tokens"]
        assert result.output_tokens == expected["output_tokens"]
        assert result.total_tokens == expected["total_tokens"]

    assert seen_workspaces
    assert all(path != tmp_path / "workspace" for path in seen_workspaces)
    assert not (tmp_path / "workspace" / "marker.txt").exists()
    assert all(not path.exists() for path in seen_workspaces)

    markdown = bench.render_markdown(comparison)
    assert re.search(r"\| claude-sdk\s+\| true\s+\| .* \| 11\s+\| 7\s+\| 18\s+\|", markdown)
    assert re.search(r"\| codex\s+\| true\s+\| .* \| 23\s+\| 5\s+\| 28\s+\|", markdown)

    payload = json.loads(bench.render_json(comparison))
    assert payload["task"] == "stub-task"
    assert payload["results"] == [result.to_dict() for result in comparison.results]


def test_run_benchmark_records_failed_success_check(tmp_path: Path) -> None:
    task_path = _write_task(
        tmp_path,
        "python -c \"import sys; print('missing marker', file=sys.stderr); raise SystemExit(7)\"",
    )

    def stub_runner(harness: str, workspace: Path, prompt: str) -> None:
        notify_from_dict(
            model="stub-model",
            usage={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        )
        (workspace / "marker.txt").write_text(f"{harness}: {prompt}\n", encoding="utf-8")

    comparison = bench.run_benchmark(bench.load_task(task_path), ["codex"], runner=stub_runner)

    result = comparison.results[0]
    assert result.harness == "codex"
    assert result.success is False
    assert result.error == "success check failed (exit 7): missing marker"
    assert result.input_tokens == 3
    assert result.output_tokens == 2
    assert result.total_tokens == 5


def test_run_benchmark_records_runner_error_and_continues(tmp_path: Path) -> None:
    check_marker = tmp_path / "success-check-ran.txt"
    task_path = _write_task(
        tmp_path,
        (
            'python -c "from pathlib import Path; '
            f"Path({str(check_marker)!r}).write_text('ran'); "
            'raise SystemExit(0)"'
        ),
    )

    def stub_runner(harness: str, workspace: Path, prompt: str) -> None:
        del workspace, prompt
        if harness == "broken":
            raise RuntimeError("boom | line\nbreak")
        notify_from_dict(
            model="stub-model",
            usage={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        )

    comparison = bench.run_benchmark(
        bench.load_task(task_path), ["broken", "ok"], runner=stub_runner
    )

    broken, ok = comparison.results
    assert broken.harness == "broken"
    assert broken.success is False
    assert broken.error == "RuntimeError: boom | line\nbreak"
    assert isinstance(broken.wall_time_s, float)
    assert broken.wall_time_s >= 0
    assert ok.harness == "ok"
    assert ok.success is True
    assert check_marker.read_text(encoding="utf-8") == "ran"


def test_run_benchmark_sums_multiple_usage_notifications(tmp_path: Path) -> None:
    task_path = _write_task(tmp_path, 'python -c "raise SystemExit(0)"')

    def stub_runner(harness: str, workspace: Path, prompt: str) -> None:
        del harness, workspace, prompt
        notify_from_dict(
            model="stub-model",
            usage={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        )
        notify_from_dict(
            model="stub-model",
            usage={"input_tokens": 7, "output_tokens": 11, "total_tokens": 18},
        )

    comparison = bench.run_benchmark(bench.load_task(task_path), ["codex"], runner=stub_runner)

    result = comparison.results[0]
    assert result.input_tokens == 9
    assert result.output_tokens == 14
    assert result.total_tokens == 23


def test_render_markdown_sanitizes_cells() -> None:
    comparison = bench.BenchComparison(
        task="task",
        results=[
            bench.BenchResult(
                harness="codex",
                success=False,
                wall_time_s=0.1,
                input_tokens=1,
                output_tokens=2,
                total_tokens=3,
                error="bad | thing\nnext\rline",
            )
        ],
    )

    markdown = bench.render_markdown(comparison)

    assert "bad \\| thing next line" in markdown
    assert "thing\nnext" not in markdown


def test_sum_usage_files_ignores_sidecars_tmp_and_junk(tmp_path: Path) -> None:
    (tmp_path / "tokens-pid1.json").write_text(
        json.dumps({"totals": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}}),
        encoding="utf-8",
    )
    (tmp_path / "tokens-pid2.json").write_text(
        json.dumps({"totals": {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12}}),
        encoding="utf-8",
    )
    (tmp_path / "tokens-current-test-main.txt").write_text("nodeid", encoding="utf-8")
    (tmp_path / "tokens-pid3.json.tmp123").write_text(
        json.dumps({"totals": {"input_tokens": 100, "output_tokens": 100, "total_tokens": 200}}),
        encoding="utf-8",
    )
    (tmp_path / "junk.json").write_text("{", encoding="utf-8")
    (tmp_path / "bad-totals.json").write_text(
        json.dumps({"totals": {"input_tokens": "not-a-number"}}),
        encoding="utf-8",
    )

    totals = bench._sum_usage_files(tmp_path)

    assert totals == bench.UsageTotals(input_tokens=6, output_tokens=9, total_tokens=15)


def test_success_check_timeout_records_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bench, "_SUCCESS_CHECK_TIMEOUT_S", 0.01)
    task_path = _write_task(
        tmp_path,
        'python -c "import time; time.sleep(1)"',
    )

    def stub_runner(harness: str, workspace: Path, prompt: str) -> None:
        del harness, workspace, prompt

    comparison = bench.run_benchmark(bench.load_task(task_path), ["codex"], runner=stub_runner)

    result = comparison.results[0]
    assert result.success is False
    assert result.error == "success check timed out after 0.01s"


def test_hung_harness_times_out_and_does_not_block_remaining_harnesses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bench, "_HARNESS_RUN_TIMEOUT_S", 0.05)
    task_path = _write_task(tmp_path, 'python -c "raise SystemExit(0)"')

    def stub_runner(harness: str, workspace: Path, prompt: str) -> None:
        del workspace, prompt
        if harness == "stuck":
            time.sleep(60)
            return
        notify_from_dict(
            model="stub-model",
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    comparison = bench.run_benchmark(
        bench.load_task(task_path), ["stuck", "ok"], runner=stub_runner
    )

    stuck, ok = comparison.results
    assert stuck.harness == "stuck"
    assert stuck.success is False
    assert stuck.error == "TimeoutError: harness 'stuck' did not finish within 0s"
    assert ok.harness == "ok"
    assert ok.success is True


def test_sample_task_loads_and_runs_with_stub() -> None:
    task = bench.load_task(_REPO_ROOT / "scripts" / "bench_tasks" / "sample.yaml")

    def stub_runner(harness: str, workspace: Path, prompt: str) -> None:
        notify_from_dict(
            model="stub-model",
            usage={"input_tokens": 13, "output_tokens": 8, "total_tokens": 21},
        )
        (workspace / "marker.txt").write_text(f"{harness}: {prompt}\n", encoding="utf-8")

    comparison = bench.run_benchmark(task, ["stub-a", "stub-b"], runner=stub_runner)

    assert task.name == "sample-marker"
    assert all(result.success for result in comparison.results)
    assert [result.total_tokens for result in comparison.results] == [21, 21]


@pytest.mark.parametrize(
    "body",
    [
        "- not-a-mapping\n",
        "name: task\nworkspace: ./workspace\nprompt: hi\nsuccess: nope\n",
        "name: task\nworkspace: ./workspace\nprompt: hi\nsuccess:\n  type: file\n  command: ok\n",
        "name: task\nworkspace: ./workspace\nprompt: hi\nsuccess:\n"
        "  type: command\n  command: ''\n",
        "workspace: ./workspace\nprompt: hi\nsuccess:\n  type: command\n  command: ok\n",
        "name: ''\nworkspace: ./workspace\nprompt: hi\nsuccess:\n  type: command\n  command: ok\n",
        "name: task\nprompt: hi\nsuccess:\n  type: command\n  command: ok\n",
        "name: task\nworkspace: ''\nprompt: hi\nsuccess:\n  type: command\n  command: ok\n",
        "name: task\nworkspace: ./workspace\nsuccess:\n  type: command\n  command: ok\n",
        "name: task\nworkspace: ./workspace\nprompt: ''\nsuccess:\n"
        "  type: command\n  command: ok\n",
        "name: task\nworkspace: ./missing\nprompt: hi\nsuccess:\n  type: command\n  command: ok\n",
    ],
)
def test_load_task_validation_errors(tmp_path: Path, body: str) -> None:
    (tmp_path / "workspace").mkdir()
    task_path = _write_yaml(tmp_path / "task.yaml", body)

    with pytest.raises(ValueError):
        bench.load_task(task_path)


def test_load_task_rejects_file_workspace(tmp_path: Path) -> None:
    (tmp_path / "workspace").write_text("not a dir", encoding="utf-8")
    task_path = _write_yaml(
        tmp_path / "task.yaml",
        "\n".join(
            [
                "name: task",
                "workspace: ./workspace",
                "prompt: hi",
                "success:",
                "  type: command",
                "  command: ok",
                "",
            ]
        ),
    )

    with pytest.raises(ValueError):
        bench.load_task(task_path)
