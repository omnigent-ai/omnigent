"""Unit tests for the cursor-native tool-approval mirror.

Covers everything a live cursor-agent isn't needed for, with the tmux + HTTP
boundaries faked:

* **Parser** — parsing an approval block out of rendered pane text
  (subject/title/keys/operation type), rejecting non-approval panes (notably
  the first-run Workspace-Trust modal, which uses ``[key]`` brackets rather
  than the ``(key)`` parentheses of a tool-approval prompt), dedup-hash
  stability, and the elicitation-id format.
* **Mirror supervisor** — ``_run_one_approval`` (park → verdict → keystroke),
  ``_post_external_elicitation_resolved`` (un-park on TUI-side answer), and
  ``supervise_cursor_approval_mirror`` (detect → spawn → release) driven with a
  fake pane capture, fake send-keys, and a stub async client.
* **Bridge helpers** — ``capture_cursor_pane`` / ``send_cursor_pane_keys`` with
  the tmux primitives monkeypatched.

The *live* tmux + cursor-agent path (real detect → POST → keystroke end-to-end)
is exercised by ``tests/e2e/test_cursor_native_cli_e2e.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import httpx
import pytest

from omnigent import cursor_native_bridge as cnb
from omnigent import cursor_native_permissions as cnp
from omnigent.cursor_native_permissions import (
    cursor_permission_elicitation_id,
    parse_cursor_approval_prompt,
)

# A faithful capture of cursor-agent's shell approval block (the widget the
# mirror keys on), as rendered by ``tmux capture-pane -p``.
_SHELL_PANE = """ $  echo omnigent_probe > out.txt in .

 Run this command?
 Shell allowlist is empty
  → Run (once) (y)
    Run Everything (shift+tab)
    Skip (esc or n)
"""

# The first-run Workspace-Trust modal — must NOT be mistaken for a tool prompt.
_TRUST_PANE = """  │  ⚠ Workspace Trust Required
  │  Do you trust the contents of this directory?
  │    [a] Trust this workspace
  │    [q] Quit
"""


def test_parse_shell_approval_extracts_subject_title_and_keys() -> None:
    """A shell approval block yields the command, title, and advertised keys."""
    prompt = parse_cursor_approval_prompt(_SHELL_PANE)

    assert prompt is not None
    assert prompt.title == "Run this command?"
    assert prompt.subject == "echo omnigent_probe > out.txt"
    assert prompt.operation_type == "shell"
    assert prompt.accept_key == "y"
    assert prompt.decline_key == "Escape"
    # The card renders the command for the user to review.
    assert "echo omnigent_probe > out.txt" in prompt.preview
    assert prompt.message


@pytest.mark.parametrize(
    "pane",
    [
        pytest.param("", id="empty"),
        pytest.param("idle\n> Add a follow-up", id="idle-input"),
        pytest.param(_TRUST_PANE, id="workspace-trust-modal"),
        pytest.param(
            " Run this command?\n  → Run (once) [y]\n    Skip [n]\n",
            id="bracket-keys-not-parens",
        ),
    ],
)
def test_parse_returns_none_for_non_approval_panes(pane: str) -> None:
    """Non-approval panes (incl. the trust modal) are not parsed as prompts."""
    assert parse_cursor_approval_prompt(pane) is None


def test_block_hash_is_stable_across_identical_captures() -> None:
    """The same prompt seen on two polls dedupes to the same hash and id."""
    first = parse_cursor_approval_prompt(_SHELL_PANE)
    second = parse_cursor_approval_prompt(_SHELL_PANE)

    assert first is not None and second is not None
    assert first.block_hash == second.block_hash


def test_block_hash_differs_for_a_different_command() -> None:
    """A different command produces a different dedup hash (a new prompt)."""
    other_pane = _SHELL_PANE.replace("echo omnigent_probe", "rm -rf build")
    first = parse_cursor_approval_prompt(_SHELL_PANE)
    other = parse_cursor_approval_prompt(other_pane)

    assert first is not None and other is not None
    assert first.block_hash != other.block_hash


def test_elicitation_id_is_deterministic_and_session_scoped() -> None:
    """The elicitation id is stable for a (session, block) and carries both."""
    eid = cursor_permission_elicitation_id("conv_abc", "deadbeef")

    assert eid == cursor_permission_elicitation_id("conv_abc", "deadbeef")
    assert eid != cursor_permission_elicitation_id("conv_xyz", "deadbeef")
    assert "conv_abc" in eid
    assert eid.startswith("elicit_cursor_")


class _QueueClient:
    """Async httpx-client stub: records POSTs, returns queued responses in order."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.posts: list[tuple[str, dict]] = []
        self._responses = list(responses)

    async def post(self, url: str, *, json: dict, **_kw: object) -> httpx.Response:
        self.posts.append((url, json))
        return self._responses.pop(0)


@pytest.mark.parametrize(
    ("response", "expected_key"),
    [
        pytest.param(httpx.Response(200, json={"action": "accept"}), "y", id="accept->y"),
        pytest.param(httpx.Response(200, json={"action": "decline"}), "Escape", id="decline->esc"),
        pytest.param(httpx.Response(200, json={"action": "cancel"}), "Escape", id="cancel->esc"),
        pytest.param(httpx.Response(200), None, id="empty-200->no-key"),
        pytest.param(httpx.Response(400, text="nope"), None, id="rejected->no-key"),
        pytest.param(httpx.Response(200, content=b"not-json"), None, id="non-json->no-key"),
        pytest.param(
            httpx.Response(200, json={"action": "??"}), None, id="unknown-action->no-key"
        ),
    ],
)
@pytest.mark.asyncio
async def test_run_one_approval_posts_then_sends_verdict_keystroke(
    response: httpx.Response,
    expected_key: str | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Park a prompt on the server, then drive the TUI with the verdict key.

    The hook POST always carries the renderable fields; a keystroke is sent ONLY
    for a concrete accept (``accept_key``) or decline/cancel (``decline_key``)
    verdict — an empty 2xx (answered in the TUI / timeout), a rejection, a
    non-JSON body, or an unknown action sends nothing.
    """
    prompt = parse_cursor_approval_prompt(_SHELL_PANE)
    assert prompt is not None
    sent: list[tuple[Path, tuple[str, ...]]] = []
    monkeypatch.setattr(cnp, "send_cursor_pane_keys", lambda d, *keys: sent.append((d, keys)))
    client = _QueueClient([response])

    await cnp._run_one_approval(
        client,  # type: ignore[arg-type]
        session_id="conv_1",
        bridge_dir=tmp_path,
        prompt=prompt,
        elicitation_id="elic_1",
    )

    url, body = client.posts[0]
    assert url == "/v1/sessions/conv_1/hooks/cursor-permission-request"
    assert body == {
        "elicitation_id": "elic_1",
        "operation_type": "shell",
        "message": prompt.message,
        "content_preview": prompt.preview,
    }
    assert sent == ([] if expected_key is None else [(tmp_path, (expected_key,))])


@pytest.mark.asyncio
async def test_post_external_elicitation_resolved_shape() -> None:
    """The un-park POST carries the resolved-event type + elicitation id."""
    client = _QueueClient([httpx.Response(200)])
    await cnp._post_external_elicitation_resolved(client, "conv_2", "elic_9")  # type: ignore[arg-type]
    url, body = client.posts[0]
    assert url == "/v1/sessions/conv_2/events"
    assert body == {
        "type": "external_elicitation_resolved",
        "data": {"elicitation_id": "elic_9"},
    }


@pytest.mark.asyncio
async def test_supervise_mirror_parks_new_prompt_then_releases_on_vanish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Detect → spawn a park task → release it when the prompt vanishes unanswered.

    Drives one full cycle: the first poll sees the shell prompt (spawns the park
    task), the next sees an empty pane while that task is still parked — the
    user answered in the TUI — so the supervisor posts
    ``external_elicitation_resolved`` to clear the web card.
    """
    created: list[object] = []

    class _FakeAsyncClient:
        def __init__(self, **_kw: object) -> None:
            self.posts: list[tuple[str, dict]] = []
            created.append(self)

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *_a: object) -> bool:
            return False

        async def post(self, url: str, *, json: dict, **_kw: object) -> httpx.Response:
            self.posts.append((url, json))
            return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(cnp.httpx, "AsyncClient", _FakeAsyncClient)

    # Pane: the prompt on the first poll, gone thereafter.
    panes = iter([_SHELL_PANE])
    monkeypatch.setattr(cnp, "capture_cursor_pane", lambda _d: next(panes, ""))

    # Hold the park task pending so the vanish path takes the release branch
    # (a completed task means the verdict already landed → no release needed).
    started = asyncio.Event()
    release = asyncio.Event()

    async def _fake_run_one(_client: object, **_kw: object) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(cnp, "_run_one_approval", _fake_run_one)

    task = asyncio.create_task(
        cnp.supervise_cursor_approval_mirror(
            base_url="http://t",
            headers={},
            session_id="conv_3",
            bridge_dir=tmp_path,
            poll_interval_s=0.001,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 2.0)
        for _ in range(400):
            if created and getattr(created[0], "posts", None):
                break
            await asyncio.sleep(0.005)
        assert created, "supervisor never opened a client"
        url, body = created[0].posts[0]  # type: ignore[attr-defined]
        assert url == "/v1/sessions/conv_3/events"
        assert body["type"] == "external_elicitation_resolved"
        prompt = parse_cursor_approval_prompt(_SHELL_PANE)
        assert prompt is not None
        assert body["data"]["elicitation_id"] == cursor_permission_elicitation_id(
            "conv_3", prompt.block_hash
        )
    finally:
        release.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_capture_cursor_pane_returns_pane_or_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pane text when the TUI is live; ``None`` when absent or the pane is dead."""
    monkeypatch.setattr(cnb, "read_tmux_info", lambda _d: {"socket_path": "s", "tmux_target": "t"})
    monkeypatch.setattr(cnb, "_session_alive", lambda _s, _t: True)
    monkeypatch.setattr(cnb, "_capture_pane", lambda _s, _t: "PANE-TEXT")
    assert cnb.capture_cursor_pane(tmp_path) == "PANE-TEXT"

    monkeypatch.setattr(cnb, "_session_alive", lambda _s, _t: False)
    assert cnb.capture_cursor_pane(tmp_path) is None  # dead pane

    monkeypatch.setattr(cnb, "read_tmux_info", lambda _d: None)
    assert cnb.capture_cursor_pane(tmp_path) is None  # no tmux target advertised


def test_send_cursor_pane_keys_invokes_tmux_send_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each key is forwarded to ``tmux send-keys -t <target>`` on the pane socket."""
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(
        cnb, "read_tmux_info", lambda _d: {"socket_path": "sock", "tmux_target": "main"}
    )
    monkeypatch.setattr(cnb, "_run_tmux", lambda sp, *a: calls.append((sp, a)))

    cnb.send_cursor_pane_keys(tmp_path, "y")
    assert calls == [("sock", ("send-keys", "-t", "main", "y"))]

    cnb.send_cursor_pane_keys(tmp_path, "Escape")
    assert calls[-1] == ("sock", ("send-keys", "-t", "main", "Escape"))


def test_send_cursor_pane_keys_raises_without_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing tmux target is a hard error (the verdict can't be delivered)."""
    monkeypatch.setattr(cnb, "read_tmux_info", lambda _d: None)
    with pytest.raises(RuntimeError):
        cnb.send_cursor_pane_keys(tmp_path, "y")
