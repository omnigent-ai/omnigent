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
import glob
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.e2e._harness_probes import cli_unavailable_reason

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
_LAUNCH_TIMEOUT = 240
_TURN_TIMEOUT = 180


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def mock_llm() -> Iterator[str]:
    """Start the shared mock model server (pi gateway + routing judge)."""
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            str(_REPO_ROOT / "tests/server/integration/mock_llm_server.py"),
            str(port),
        ],
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    ready = False
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            if httpx.get(f"{base}/stats", timeout=1).status_code == 200:
                ready = True
                break
        time.sleep(0.2)
    if not ready:
        proc.kill()
        pytest.skip("mock model server did not boot")
    # A plain assistant reply for either model id, so a served turn always
    # completes regardless of which model the pane ends up on.
    for key, text in (
        (_DEFAULT_MODEL, "PONG-DEFAULT"),
        (_TARGET_MODEL, "PONG-TARGET"),
        ("default", "PONG"),
    ):
        httpx.post(f"{base}/mock/set_fallback", json={"key": key, "text": text}, timeout=10)
    try:
        yield base
    finally:
        proc.send_signal(signal.SIGTERM)
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


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
    env = {
        **os.environ,
        "HOME": str(pi_home),
        "OMNIGENT_CONFIG_HOME": str(pi_home / ".omnigent"),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "PYTHONPATH": os.pathsep.join(
            str(p)
            for p in (
                _REPO_ROOT,
                _REPO_ROOT / "sdks" / "python-client",
                _REPO_ROOT / "sdks" / "ui",
            )
        ),
        "TERM": "xterm-256color",
        "PROMPT_TOOLKIT_NO_CPR": "1",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    env.pop("OMNIGENT_CONFIG", None)
    return env


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
    child.expect(r"Web UI:\s*(\S+)", timeout=_LAUNCH_TIMEOUT)
    web_url = child.match.group(1)
    match = re.match(r"(https?://[^/]+)/c/(\S+)", web_url)
    assert match, f"could not parse Web UI url: {web_url!r}"
    return child, match.group(1), match.group(2)


def _find_pi_pane() -> tuple[str, str] | None:
    """Locate the runner-owned tmux socket + pane hosting the pi TUI.

    The pi TUI runs in a runner-owned tmux pane; a keystroke typed by the user
    reaches it through that pane, so ``tmux send-keys`` is how we type - a
    pexpect stdin write to ``omnigent pi`` never reaches the pane's input box.
    """
    socks = set(glob.glob("/tmp/**/omnigent-terminal-*/tmux.sock", recursive=True))
    with contextlib.suppress(Exception):
        out = subprocess.run(
            ["ps", "-eo", "args"], capture_output=True, text=True, timeout=10
        ).stdout
        for line in out.splitlines():
            m = re.search(r"-S\s+(\S+tmux\.sock)", line)
            if m:
                socks.add(m.group(1))
    for sock in sorted(socks):
        with contextlib.suppress(Exception):
            panes = subprocess.run(
                ["tmux", "-S", sock, "list-panes", "-a", "-F", "#{pane_id}"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.split()
            if panes:
                return sock, panes[0]
    return None


def _tmux_type(pane: tuple[str, str], text: str) -> None:
    sock, pane_id = pane
    subprocess.run(
        ["tmux", "-S", sock, "send-keys", "-t", pane_id, "-l", text], check=False, timeout=10
    )
    time.sleep(0.5)
    subprocess.run(
        ["tmux", "-S", sock, "send-keys", "-t", pane_id, "Enter"], check=False, timeout=10
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
    )
    if _snapshot(server, conv).get("model_override"):
        httpx.patch(f"{server}/v1/sessions/{conv}", json={"model_override": None}, timeout=30)


def _configure_judge_from_options(mock: str, server: str, conv: str) -> None:
    """Point the routing judge at a real non-default pushed model option.

    The pane pushes its ``external_model_options`` on start; the snapshot
    serves them. The router clamps an unknown pick to the cheapest candidate,
    so the judge must return an id that is actually in the menu. Falls back to
    the bare target id when options are not yet exposed (the buggy build fails
    either way, having no candidates at all).
    """
    pick = _TARGET_MODEL
    deadline = time.monotonic() + 90
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
    )


def _await_served_turn(mock: str, before: int) -> list[str]:
    deadline = time.monotonic() + _TURN_TIMEOUT
    while time.monotonic() < deadline:
        served = _served_pi_models(mock)
        if len(served) > before:
            return served[before:]
        time.sleep(2)
    return []


def _diagnostics(mock: str, server: str, conv: str, served: list[str]) -> str:
    return (
        f"served models for the turn={served!r}; "
        f"judge calls={_judge_call_count(mock)}; "
        f"routing_decision items={_routing_decisions(server, conv)!r}; "
        f"model_override={_snapshot(server, conv).get('model_override')!r}"
    )


def test_pi_native_smart_routing_composer_turn_applies_pick(pi_home: Path, mock_llm: str) -> None:
    """A composer (web) turn with Smart Routing on runs on the ROUTED model.

    Buggy build: the router finds no pi-native candidates, is never consulted,
    records no decision, and the turn is served on the default model.
    """
    env = _env(pi_home)
    child, server, conv = _launch_pi(env)
    try:
        child.expect(_DEFAULT_MODEL, timeout=_LAUNCH_TIMEOUT)
        time.sleep(8)  # let the pane's model registry push its options
        _enable_smart_routing(server, conv)
        _configure_judge_from_options(mock_llm, server, conv)

        before = len(_served_pi_models(mock_llm))
        resp = httpx.post(
            f"{server}/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": _COMPLEX_PROMPT,
                        }
                    ],
                },
            },
            timeout=60,
        )
        assert resp.status_code in (200, 202), (
            f"composer POST failed: {resp.status_code} {resp.text[:300]}"
        )

        served = _await_served_turn(mock_llm, before)
        diag = _diagnostics(mock_llm, server, conv, served)
        assert served, (
            f"composer turn produced no served pi request within {_TURN_TIMEOUT}s. {diag}"
        )
        assert _TARGET_MODEL in served, (
            "Smart Routing pick was not applied to the pi-native composer turn: "
            f"the turn was served on the default model, not the routed {_TARGET_MODEL!r}. {diag}"
        )
    finally:
        _teardown(child, env)


def test_pi_native_smart_routing_tui_prompt_is_routed(pi_home: Path, mock_llm: str) -> None:
    """A prompt typed into the Pi TUI with Smart Routing on runs on the ROUTED model.

    Buggy build: a TUI-typed prompt bypasses the route gate entirely (no
    ``UserPromptSubmit`` hook for pi-native), so the judge is never consulted
    and the turn is served on the default model.
    """
    env = _env(pi_home)
    child, server, conv = _launch_pi(env)
    try:
        child.expect(_DEFAULT_MODEL, timeout=_LAUNCH_TIMEOUT)
        time.sleep(10)  # let the runner-owned pane + extension settle
        _enable_smart_routing(server, conv)
        _configure_judge_from_options(mock_llm, server, conv)

        pane = _find_pi_pane()
        assert pane is not None, "could not locate the runner-owned pi tmux pane to type into"

        judge_before = _judge_call_count(mock_llm)
        before = len(_served_pi_models(mock_llm))
        _tmux_type(pane, _COMPLEX_PROMPT)

        served = _await_served_turn(mock_llm, before)
        diag = _diagnostics(mock_llm, server, conv, served)
        assert served, (
            f"TUI-typed prompt produced no served pi request within {_TURN_TIMEOUT}s. {diag}"
        )
        assert _judge_call_count(mock_llm) > judge_before, (
            "the TUI-typed prompt was never routed: the routing judge was not "
            f"consulted (no UserPromptSubmit route-turn hook for pi-native). {diag}"
        )
        assert _TARGET_MODEL in served, (
            "Smart Routing pick was not applied to the pi-native TUI turn: "
            f"the turn was served on the default model, not the routed {_TARGET_MODEL!r}. {diag}"
        )
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
    omni_bin = Path(sys.executable).parent / "omni"
    if omni_bin.exists():
        with contextlib.suppress(Exception):
            subprocess.run(
                [str(omni_bin), "server", "stop"], env=env, capture_output=True, timeout=60
            )
