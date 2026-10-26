"""CLI e2e: Smart Routing picks must be applied to a running pi-native session.

Drives the *real* ``omnigent pi`` CLI under a pseudo-TTY (pexpect), against an
auto-spawned local server + runner + real ``pi`` TUI. With Smart Routing on, a
running pi-native session must actually switch to the model the router picks -
for a composer (web) turn (the routed ``model_override`` must reach the pane as
a model switch ahead of the message) and for a prompt typed straight into the
Pi TUI (which is invisible to the composer route gate and must be routed
through the route-turn hook), with the pane's own pushed
``external_model_options`` served as the routing candidate catalog.

The observable, cross-facet contract: with Smart Routing on and a judge that
routes to the non-default model, the pi turn is served on the ROUTED model. On
a regressed build the judge is never consulted, no routing decision is
recorded, and the turn is served on the default model - so these tests fail.

The mock routing judge is configured from the session's own live pushed model
options (read from the snapshot), so its pick is always a real menu entry - the
routing client clamps an unknown model to the cheapest candidate, which would
otherwise mask the fix.

Modelled on ``tests/e2e/test_pi_native_gateway_claude_misroute_e2e.py`` (a proven
pexpect + fake ``HOME`` + mock-model-server CLI e2e).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests._helpers.live_server import isolated_process_env
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.conftest import configure_mock_llm, get_mock_requests
from tests.e2e.omnigent._pexpect_harness import strip_ansi

pexpect = pytest.importorskip("pexpect")

pytestmark = pytest.mark.skipif(
    (_reason := cli_unavailable_reason("pi")) is not None,
    reason=f"pi-native smart-routing e2e requires a runnable 'pi' CLI; {_reason}.",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
# Two OpenAI-family models served over the same chat wire (no cross-family
# misroute): the session's spawn default, and the model the judge routes to.
_DEFAULT_MODEL = "gpt-fake-5-1"
_TARGET_MODEL = "gpt-fake-5-2"
_JUDGE_MODEL = "mock-judge"
_COMPLEX_PROMPT = "Refactor the whole module and add tests; this is a complex, multi-file task."
_LAUNCH_TIMEOUT = 120
_TURN_TIMEOUT = 60


@pytest.fixture
def mock_llm(isolated_mock_llm_server_url: str) -> str:
    """Script the existing isolated mock for the pane and routing judge."""
    for key, text in (
        (_DEFAULT_MODEL, "PONG-DEFAULT"),
        (_TARGET_MODEL, "PONG-TARGET"),
        ("default", "PONG"),
    ):
        response = httpx.post(
            f"{isolated_mock_llm_server_url}/mock/set_fallback",
            json={"key": key, "text": text},
            timeout=10,
        )
        response.raise_for_status()
    configure_mock_llm(
        isolated_mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "routing-tool",
                        "name": "bash",
                        "arguments": '{"command":"printf PI_ROUTING_TOOL_RESULT"}',
                    }
                ]
            },
            {"text": "ROUTED_FIRST_ANSWER", "stream": True},
            {"text": "ROUTED_FOLLOWUP_ANSWER", "stream": True},
        ],
        key=_TARGET_MODEL,
    )
    return isolated_mock_llm_server_url


@pytest.fixture
def pi_home(tmp_path: Path, mock_llm: str) -> Path:
    """A fake ``HOME`` wiring the pi gateway and the routing judge to the mock."""
    config_home = tmp_path / ".omnigent"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        "auto_open_conversation: false\n"
        "providers:\n"
        "  repro-gw:\n"
        "    kind: gateway\n"
        "    default: true\n"
        "    openai:\n"
        f"      base_url: {mock_llm}/v1\n"
        "      api_key: synthetic-repro-key\n"
        "      wire_api: chat\n"
        "      models:\n"
        f"        default: {_DEFAULT_MODEL}\n"
        f"        balanced: {_TARGET_MODEL}\n"
        f"        quality: {_TARGET_MODEL}\n"
        "llm:\n"
        f"  model: {_JUDGE_MODEL}\n"
        "  connection:\n"
        f"    base_url: {mock_llm}/v1\n"
        "    api_key: mock-key\n"
    )
    return tmp_path


def _env(pi_home: Path) -> dict[str, str]:
    base_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OMNIGENT_", "RUNNER_", "PI_", "OPENAI_", "ANTHROPIC_"))
        and key != "TMUX"
    }
    return isolated_process_env(
        pi_home,
        {
            "OMNIGENT_CONFIG_HOME": str(pi_home / ".omnigent"),
            "OMNIGENT_PI_PATH": os.environ.get("OMNIGENT_PI_PATH"),
            "OMNIGENT_SKIP_ONBOARD": "1",
            "OMNIGENT_NO_UPDATE_CHECK": "1",
            "PI_OFFLINE": "1",
            "PI_TELEMETRY": "0",
            "TERM": "xterm-256color",
            "PROMPT_TOOLKIT_NO_CPR": "1",
        },
        base_env,
    )


def _launch_pi(env: dict[str, str]) -> tuple[Any, str, str]:
    omnigent_bin = Path(sys.executable).parent / "omnigent"
    assert omnigent_bin.exists(), f"omnigent CLI not found at {omnigent_bin}"
    child = pexpect.spawn(
        str(omnigent_bin),
        ["pi", "--server", ""],  # auto-spawn a local server + runner
        cwd=str(_REPO_ROOT),
        env=env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 140),
        timeout=_LAUNCH_TIMEOUT,
    )
    # Under the logs dir so the E2E job uploads the transcript with the other logs.
    transcript = Path(env["HOME"]) / ".omnigent" / "logs" / "pi-terminal.log"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    child.logfile_read = transcript.open("w")
    try:
        child.expect(r"Web UI:\s*(\S+)", timeout=_LAUNCH_TIMEOUT)
        web_url = child.match.group(1)
        match = re.match(r"(https?://[^/]+)/c/(\S+)", web_url)
        assert match, f"could not parse Web UI url: {web_url!r}"
        return child, match.group(1), match.group(2)
    except pexpect.TIMEOUT as exc:
        child.logfile_read.flush()
        output = child.before or ""
        _teardown(child, env)
        raise AssertionError(
            f"omnigent pi printed no Web UI line within {_LAUNCH_TIMEOUT}s "
            f"('Web UI' in the raw output: {'Web UI' in output}); transcript: {transcript}\n"
            f"{strip_ansi(output)[:2000]}\n...\n{strip_ansi(output)[-1000:]}"
        ) from exc
    except BaseException:
        _teardown(child, env)
        raise


def _find_pi_pane(server: str, conv: str) -> tuple[str, str]:
    """Use only the terminal registered to this test's session."""
    response = httpx.get(
        f"{server}/v1/sessions/{conv}/resources/terminals/terminal_pi_main", timeout=10
    )
    response.raise_for_status()
    metadata = response.json()["metadata"]
    return metadata["tmux_socket"], metadata["tmux_target"]


def _tmux_type(pane: tuple[str, str], text: str) -> None:
    sock, pane_id = pane
    subprocess.run(
        ["tmux", "-S", sock, "send-keys", "-t", pane_id, "-l", text], check=True, timeout=10
    )
    time.sleep(0.5)
    subprocess.run(
        ["tmux", "-S", sock, "send-keys", "-t", pane_id, "Enter"], check=True, timeout=10
    )


def _snapshot(server: str, conv: str) -> dict[str, Any]:
    resp = httpx.get(f"{server}/v1/sessions/{conv}", timeout=30)
    resp.raise_for_status()
    return resp.json()


def _routing_decisions(server: str, conv: str) -> list[dict[str, Any]]:
    return [
        it
        for it in _snapshot(server, conv).get("items", [])
        if it.get("type") == "routing_decision"
    ]


def _served_pi_models(mock: str) -> list[str]:
    """Model ids the pi gateway was asked to serve (excludes the judge)."""
    reqs = httpx.get(f"{mock}/mock/requests", timeout=10).json().get("requests", [])
    return [
        r.get("model")
        for r in reqs
        if isinstance(r, dict) and r.get("model") in (_DEFAULT_MODEL, _TARGET_MODEL)
    ]


def _judge_call_count(mock: str) -> int:
    return len(
        httpx.get(f"{mock}/mock/requests?key={_JUDGE_MODEL}", timeout=10)
        .json()
        .get("requests", [])
    )


def _enable_smart_routing(server: str, conv: str) -> None:
    httpx.patch(
        f"{server}/v1/sessions/{conv}", json={"cost_control_mode_override": "on"}, timeout=30
    ).raise_for_status()
    if _snapshot(server, conv).get("model_override"):
        httpx.patch(
            f"{server}/v1/sessions/{conv}", json={"model_override": None}, timeout=30
        ).raise_for_status()


def _configure_judge_from_options(mock: str, server: str, conv: str) -> None:
    """Point the routing judge at a real non-default pushed model option.

    The pane pushes its ``external_model_options`` on start; the snapshot
    serves them. Require the target in that menu so an unknown-model fallback
    cannot mask the routing behavior.
    """
    pick = None
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        options = _snapshot(server, conv).get("model_options") or []
        target = next(
            (
                str(opt.get("id"))
                for opt in options
                if isinstance(opt, dict)
                and isinstance(opt.get("id"), str)
                and str(opt["id"]).split("/")[-1] == _TARGET_MODEL
            ),
            None,
        )
        if target:
            pick = target
            break
        time.sleep(3)
    assert pick is not None, "Pi did not publish the target model in its live options"
    verdict = json.dumps(
        {
            "harness": "pi-native",
            "model": pick,
            "rationale": f"This is a COMPLEX task (e2e); selected most capable model {pick}.",
        }
    )
    httpx.post(
        f"{mock}/mock/configure",
        json={"key": _JUDGE_MODEL, "responses": [{"text": verdict}] * 12},
        timeout=10,
    ).raise_for_status()


def _await_answer(child: Any, server: str, conv: str, marker: str) -> list[dict[str, Any]]:
    deadline = time.monotonic() + _TURN_TIMEOUT
    items: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        try:
            child.read_nonblocking(65536, timeout=0.1)
        except pexpect.TIMEOUT:
            pass
        except pexpect.EOF as exc:
            raise AssertionError(
                f"Pi CLI exited before {marker}; last mirrored items: {items[-8:]}"
            ) from exc
        response = httpx.get(f"{server}/v1/sessions/{conv}/items", timeout=10)
        response.raise_for_status()
        items = response.json().get("data", [])
        if any(item.get("role") == "assistant" and marker in json.dumps(item) for item in items):
            return items
        time.sleep(0.2)
    raise AssertionError(f"Pi did not mirror {marker}: {items[-8:]}")


def _diagnostics(mock: str, server: str, conv: str, served: list[str]) -> str:
    return (
        f"served models for the turn={served!r}; "
        f"judge calls={_judge_call_count(mock)}; "
        f"routing_decision items={_routing_decisions(server, conv)!r}; "
        f"model_override={_snapshot(server, conv).get('model_override')!r}"
    )


@pytest.mark.parametrize("surface", ["composer", "tui"])
def test_pi_native_smart_routing_applies_pick(pi_home: Path, mock_llm: str, surface: str) -> None:
    """Route before inference, then preserve the tool result on the next turn."""
    env = _env(pi_home)
    child, server, conv = _launch_pi(env)
    try:
        child.expect(_DEFAULT_MODEL, timeout=_LAUNCH_TIMEOUT)
        _enable_smart_routing(server, conv)
        _configure_judge_from_options(mock_llm, server, conv)
        pane = _find_pi_pane(server, conv) if surface == "tui" else None

        def send(text: str) -> None:
            if pane is not None:
                _tmux_type(pane, text)
            else:
                response = httpx.post(
                    f"{server}/v1/sessions/{conv}/events",
                    json={
                        "type": "message",
                        "data": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": text}],
                        },
                    },
                    timeout=30,
                )
                assert response.status_code in (200, 202), response.text

        judge_before = _judge_call_count(mock_llm)
        send(_COMPLEX_PROMPT)
        _await_answer(child, server, conv, "ROUTED_FIRST_ANSWER")
        served = _served_pi_models(mock_llm)
        assert served == [_TARGET_MODEL, _TARGET_MODEL], _diagnostics(
            mock_llm, server, conv, served
        )
        assert _judge_call_count(mock_llm) > judge_before
        judge_after = _judge_call_count(mock_llm)

        send("What did the command return?")
        items = _await_answer(child, server, conv, "ROUTED_FOLLOWUP_ANSWER")
        for prompt in (_COMPLEX_PROMPT, "What did the command return?"):
            assert (
                sum(
                    item.get("role") == "user"
                    and any(part.get("text") == prompt for part in item.get("content", []))
                    for item in items
                )
                == 1
            ), items
        assert _served_pi_models(mock_llm) == [_TARGET_MODEL] * 3
        assert _judge_call_count(mock_llm) == judge_after
        requests = get_mock_requests(mock_llm, key=_TARGET_MODEL)
        history = requests[-1]["messages"]
        assert any(
            message.get("role") == "tool"
            and message.get("tool_call_id") == "routing-tool"
            and message.get("content") == "PI_ROUTING_TOOL_RESULT"
            for message in history
        )
        assert any(
            message.get("role") == "assistant" and message.get("content") == "ROUTED_FIRST_ANSWER"
            for message in history
        )
        (pi_home / "routing-evidence.json").write_text(
            json.dumps({"surface": surface, "requests": requests, "items": items}, indent=2)
        )
    except BaseException:
        with contextlib.suppress(Exception):
            (pi_home / "routing-failure.json").write_text(
                json.dumps(
                    {"requests": get_mock_requests(mock_llm), "session": _snapshot(server, conv)},
                    indent=2,
                )
            )
        raise
    finally:
        _teardown(child, env)


def _teardown(child: Any, env: dict[str, str]) -> None:
    """Detach the CLI and stop the auto-spawned server + local daemon.

    A broad ``pkill`` is deliberately avoided: this test's own filename on the
    pytest command line contains ``pi_native``, so a broad pattern would kill
    the runner. ``omni server stop`` reaps the runner-owned Pi/tmux via the daemon.
    """
    with contextlib.suppress(Exception):
        child.kill(signal.SIGTERM)
    time.sleep(2)
    with contextlib.suppress(Exception):
        child.kill(signal.SIGKILL)
    with contextlib.suppress(Exception):
        child.close(force=True)
    if child.logfile_read is not None:
        with contextlib.suppress(Exception):
            child.logfile_read.close()
    omni_bin = Path(sys.executable).parent / "omni"
    if omni_bin.exists():
        with contextlib.suppress(Exception):
            subprocess.run(
                [str(omni_bin), "server", "stop"], env=env, capture_output=True, timeout=60
            )
