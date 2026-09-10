"""End-to-end coverage for Context Saver's Focused Read happy path."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import httpx
import yaml

from tests.e2e._run_with_group_timeout import run_with_group_timeout
from tests.e2e.omnigent.conftest import configure_mock_llm, reset_mock_llm

_PRIMARY_MODEL = "mock-context-saver-main"
_WORKER_MODEL = "mock-context-saver-worker"
_TARGET_FILE = "context-saver-e2e.txt"
_TARGET_VALUE = "CONTEXT_SAVER_E2E_TARGET=orchid-731"
_TARGET_LINE = 55
_RUN_TIMEOUT_SECONDS = 120


def _tool_result_payloads(requests: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return structured tool outputs carried into primary-model requests."""
    results: list[dict[str, object]] = []
    for request in requests:
        items = request.get("input")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "function_call_output":
                continue
            output = item.get("output")
            if isinstance(output, dict):
                results.append(output)
                continue
            if not isinstance(output, str):
                continue
            try:
                parsed = json.loads(output)
            except json.JSONDecodeError:
                try:
                    # openai-agents currently carries runner dict outputs
                    # forward using Python's representation.
                    parsed = ast.literal_eval(output)
                except (SyntaxError, ValueError):
                    continue
            if isinstance(parsed, dict):
                results.append(parsed)
    return results


def test_context_saver_redirects_a_large_read_to_the_configured_worker(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """A broad read is blocked and Focused Read returns a validated excerpt."""
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_read",
                        "name": "sys_os_read",
                        "arguments": json.dumps({"path": _TARGET_FILE}),
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "call_id": "call_focus",
                        "name": "sys_context_read",
                        "arguments": json.dumps(
                            {
                                "paths": [_TARGET_FILE],
                                "question": "What is the CONTEXT_SAVER_E2E_TARGET value?",
                            }
                        ),
                    }
                ]
            },
            {"text": f"FOUND {_TARGET_VALUE}"},
        ],
        key=_PRIMARY_MODEL,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "text": json.dumps(
                    {
                        "answer": _TARGET_VALUE,
                        "sources": [
                            {
                                "path": _TARGET_FILE,
                                "ranges": [
                                    {
                                        "start": _TARGET_LINE,
                                        "end": _TARGET_LINE,
                                        "excerpt": _TARGET_VALUE,
                                    }
                                ],
                            }
                        ],
                    }
                )
            }
        ],
        key=_WORKER_MODEL,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lines = [f"filler line {line}" for line in range(1, 61)]
    lines[_TARGET_LINE - 1] = _TARGET_VALUE
    (workspace / _TARGET_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")

    config_home = tmp_path / "config"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "auth": {"type": "api_key"},
                "providers": {
                    "context-saver-e2e": {
                        "kind": "key",
                        "openai": {
                            "base_url": f"{mock_llm_server_url}/v1",
                            "api_key": "mock-key",
                        },
                    }
                },
                "context_saver": {
                    "enabled": True,
                    "techniques": {
                        "focused_read": {
                            "enabled": True,
                            "min_lines": 20,
                            "max_excerpt_lines": 5,
                            "worker_model": f"openai/{_WORKER_MODEL}",
                            "worker_provider": "context-saver-e2e",
                            "allow_source_upload": True,
                        }
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    env = dict(mock_credentials_env)
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    env["OMNIGENT_WRAPPER_BYPASS"] = "1"
    result = run_with_group_timeout(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(
                omnigent_repo_root / "tests" / "resources" / "examples" / "agent_with_os_env.yaml"
            ),
            "--model",
            _PRIMARY_MODEL,
            "--harness",
            "openai-agents",
            "-p",
            f"Read {_TARGET_FILE} and report CONTEXT_SAVER_E2E_TARGET.",
            "--no-log",
            "--no-session",
        ],
        env=env,
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SECONDS,
    )

    assert result.returncode == 0, (
        f"Context Saver run failed; stdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    assert _TARGET_VALUE in result.stdout

    response = httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=5.0)
    response.raise_for_status()
    requests = response.json()["requests"]
    primary_requests = [request for request in requests if request.get("model") == _PRIMARY_MODEL]
    worker_requests = [request for request in requests if request.get("model") == _WORKER_MODEL]

    assert len(primary_requests) == 3
    assert len(worker_requests) == 1
    assert all(_TARGET_VALUE not in json.dumps(request) for request in primary_requests[:2])
    assert _TARGET_VALUE in json.dumps(worker_requests[0])

    tool_results = _tool_result_payloads(primary_requests)
    redirect = next(
        payload for payload in tool_results if payload.get("context_saver") == "redirect"
    )
    assert redirect["reason"] == "focused_read_available"
    assert redirect["paths"] == [_TARGET_FILE]
    assert _TARGET_VALUE not in repr(redirect)

    focused = next(
        payload for payload in tool_results if payload.get("technique") == "focused_read"
    )
    assert focused["failure"] is None
    assert _TARGET_VALUE in focused["content"]
    assert focused["source_paths"] == [_TARGET_FILE]
    assert focused["relevant_line_ranges"] == {_TARGET_FILE: [[_TARGET_LINE, _TARGET_LINE]]}
    assert focused["model_routing"] == {
        "primary_models": "all",
        "worker_route": f"openai/{_WORKER_MODEL}",
        "worker_model_reported": _WORKER_MODEL,
        "route_provider": "openai",
        "non_databricks_source_sharing_allowed": True,
    }
