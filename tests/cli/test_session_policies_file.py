"""Tests for ``--policies FILE`` (:mod:`omnigent.session_policies_file`).

Covers:

- Loading: a valid file becomes one request body per policy; bad YAML, a
  missing ``policies`` list, an invalid entry, and a repeated name each fail
  with a message naming the file.
- Attaching: one POST per policy to the session's policies endpoint; a 409
  (already attached, e.g. on resume) is kept; any other error stops the launch.
- Pi launch: policies are attached after the session exists and before the
  runner starts, for new and resumed sessions.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import click
import httpx
import pytest

from omnigent.harnesses.pi_native import main as pi_main
from omnigent.session_policies_file import apply_session_policies, load_policies_file

_HANDLER = "omnigent.policies.builtins.safety.max_tool_calls_per_session"
_POLICY: dict[str, object] = {
    "name": "cap-tool-calls",
    "type": "python",
    "handler": _HANDLER,
    "factory_params": {"limit": 20},
}


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "policies.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# ── load_policies_file ───────────────────────────────────────────────────────


def test_load_returns_one_body_per_policy(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        f"""
policies:
  - name: cap-tool-calls
    type: python
    handler: {_HANDLER}
    factory_params: {{limit: 20}}
  - name: remote-check
    type: url
    handler: https://example.com/policies/eval
""",
    )
    assert load_policies_file(path) == [
        _POLICY,
        {"name": "remote-check", "type": "url", "handler": "https://example.com/policies/eval"},
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("policies: [unclosed", "not valid YAML"),
        ("- just a list", "expected a top-level 'policies:' list"),
        ("policies: {name: x}", "expected a top-level 'policies:' list"),
        ("policies: [plain-string]", "entry 0 is not a mapping"),
        ("policies: [{name: x, type: python}]", "entry 0 is invalid: handler"),
        ("policies: [{name: x, type: shell, handler: a.b}]", "type must be 'python' or 'url'"),
        ("policies: [{name: x, type: url, handler: 'http://plain'}]", "https://"),
    ],
)
def test_load_rejects_bad_files_with_a_clear_message(
    tmp_path: Path, text: str, expected: str
) -> None:
    path = _write(tmp_path, text)
    with pytest.raises(click.ClickException) as excinfo:
        load_policies_file(path)
    assert expected in excinfo.value.message
    assert str(path) in excinfo.value.message


def test_load_rejects_a_repeated_name(tmp_path: Path) -> None:
    """The server would 409 the second one; catch it before launching."""
    path = _write(
        tmp_path,
        f"policies: [{{name: a, type: python, handler: {_HANDLER}}},"
        f" {{name: a, type: python, handler: {_HANDLER}}}]",
    )
    with pytest.raises(click.ClickException, match="appears twice"):
        load_policies_file(path)


# ── apply_session_policies ───────────────────────────────────────────────────


def _client(statuses: list[int], seen: list[httpx.Request]) -> httpx.AsyncClient:
    """Client whose responses come from *statuses* in order; records requests."""
    replies = iter(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(next(replies), json={"detail": "server said no"})

    return httpx.AsyncClient(base_url="http://omnigent", transport=httpx.MockTransport(handler))


def test_apply_posts_each_policy_to_the_session() -> None:
    seen: list[httpx.Request] = []
    second: dict[str, object] = {
        "name": "other",
        "type": "url",
        "handler": "https://example.com/p",
    }

    async def go() -> None:
        async with _client([201, 201], seen) as client:
            await apply_session_policies(client, "conv_abc123", [_POLICY, second])

    asyncio.run(go())
    assert [r.method for r in seen] == ["POST", "POST"]
    assert {r.url.path for r in seen} == {"/v1/sessions/conv_abc123/policies"}
    assert [json.loads(r.content) for r in seen] == [_POLICY, second]


def test_apply_keeps_an_already_attached_policy(capsys: pytest.CaptureFixture[str]) -> None:
    """Resuming with the same file must not fail on the policies it attached last time."""
    seen: list[httpx.Request] = []

    async def go() -> None:
        async with _client([409], seen) as client:
            await apply_session_policies(client, "conv_abc123", [_POLICY])

    asyncio.run(go())
    assert "already attached" in capsys.readouterr().err


def test_apply_stops_on_a_rejected_policy() -> None:
    """An unregistered handler (server 400) must not launch an unguarded session."""
    seen: list[httpx.Request] = []

    async def go() -> None:
        async with _client([400, 201], seen) as client:
            await apply_session_policies(
                client, "conv_abc123", [_POLICY, {**_POLICY, "name": "second"}]
            )

    with pytest.raises(click.ClickException, match=r"cap-tool-calls.*\(400\).*server said no"):
        asyncio.run(go())
    assert len(seen) == 1, "Nothing after the rejected policy may be attached."


# ── Pi launch ordering ───────────────────────────────────────────────────────


def _fake_pi_launch(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    """Replace the server, runner and terminal steps of the Pi launch with recorders."""

    @asynccontextmanager
    async def fake_client(*_args: Any, **_kwargs: Any) -> AsyncIterator[object]:  # type: ignore[explicit-any]
        yield object()

    async def create(*_args: Any, **_kwargs: Any) -> str:  # type: ignore[explicit-any]
        events.append("create-session")
        return "conv_new"

    async def fetch(*_args: Any, **_kwargs: Any) -> dict[str, object]:  # type: ignore[explicit-any]
        return {"labels": {pi_main._WRAPPER_LABEL_KEY: pi_main._WRAPPER_LABEL_VALUE}}

    async def noop(*_args: Any, **_kwargs: Any) -> None:  # type: ignore[explicit-any]
        return None

    async def none_running(*_args: Any, **_kwargs: Any) -> None:  # type: ignore[explicit-any]
        return None

    async def launch_runner(*_args: Any, **_kwargs: Any) -> str:  # type: ignore[explicit-any]
        events.append("launch-runner")
        return "runner_1"

    async def terminal_ready(*_args: Any, **_kwargs: Any) -> pi_main.LaunchedPiTerminal:  # type: ignore[explicit-any]
        return pi_main.LaunchedPiTerminal(terminal_id="t1", tmux_socket=None, tmux_target=None)

    async def attach(_client: object, session_id: str, policies: list[dict[str, object]]) -> None:
        events.append(f"attach-policies:{session_id}:{len(policies)}")

    monkeypatch.setattr(pi_main, "open_daemon_client", fake_client)
    monkeypatch.setattr(pi_main, "_create_pi_session", create)
    monkeypatch.setattr(pi_main, "_fetch_pi_session", fetch)
    monkeypatch.setattr(pi_main, "_find_running_pi_terminal", none_running)
    monkeypatch.setattr(pi_main, "wait_for_host_online", noop)
    monkeypatch.setattr(pi_main, "launch_or_reuse_daemon_runner", launch_runner)
    monkeypatch.setattr(pi_main, "wait_for_runner_online", noop)
    monkeypatch.setattr(pi_main, "_bind_session_runner", noop)
    monkeypatch.setattr(pi_main, "_ensure_pi_terminal_on_runner", noop)
    monkeypatch.setattr(pi_main, "_wait_for_pi_terminal_ready", terminal_ready)
    monkeypatch.setattr(pi_main, "apply_session_policies", attach)


def _prepare(session_id: str | None, policies: list[dict[str, object]] | None) -> None:
    asyncio.run(
        pi_main._prepare_pi_terminal_via_daemon(
            base_url="http://omnigent",
            headers={},
            session_id=session_id,
            session_bundle=b"bundle",
            pi_args=(),
            host_id="host_1",
            workspace="/work",
            policies=policies,
        )
    )


def test_new_pi_session_gets_policies_before_the_runner_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _fake_pi_launch(monkeypatch, events)
    _prepare(None, [_POLICY])
    assert events == ["create-session", "attach-policies:conv_new:1", "launch-runner"]


def test_resumed_pi_session_gets_policies_before_the_runner_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _fake_pi_launch(monkeypatch, events)
    _prepare("conv_old", [_POLICY])
    assert events == ["attach-policies:conv_old:1", "launch-runner"]


def test_no_policies_flag_attaches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    _fake_pi_launch(monkeypatch, events)
    _prepare(None, None)
    assert events == ["create-session", "launch-runner"]
