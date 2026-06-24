"""
Tests for :mod:`omnigent.resume_dispatch` — the top-level
``omnigent resume`` dispatcher.

The dispatcher's job is to translate the user's "take me back to
where I was" intent into the right wrapper call. The two important
properties under test are (a) we always preserve the Omnigent
conversation id end-to-end (no new id minted on resume) and (b)
claude-native conversations route to ``run_claude_native``,
everything else surfaces a clear redirect hint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click
import httpx
import pytest

from omnigent import resume_dispatch

# ── run_resume — top-level entry ──────────────────────────


def test_run_resume_picker_form_requires_server() -> None:
    """
    ``omnigent resume`` (no conv id, no --server) must fail loud.

    Without ``target`` we'd open the cross-agent picker; without
    ``--server`` we have no Omnigent endpoint to query. Starting an
    empty local server just for the picker would race with any
    other ``omnigent`` process the user has running, so we
    redirect via UsageError instead of silently doing it.
    """
    with pytest.raises(click.UsageError) as excinfo:
        resume_dispatch.run_resume(target=None, server=None)
    # Message names both ways out of the error: a conv id OR --server.
    assert "conv_" in str(excinfo.value)
    assert "--server" in str(excinfo.value)


def test_run_resume_picker_cancel_exits_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Picker returns ``None`` (user pressed q / Enter on empty list)
    → dispatcher MUST return cleanly without calling
    ``run_claude_native``. A misroute that called the wrapper with
    ``session_id=None`` would silently create a fresh session the
    user explicitly chose not to create.
    """
    monkeypatch.setattr(
        resume_dispatch,
        "_pick_conversation_for_resume",
        lambda *, server: None,
    )
    invoked: list[str] = []

    def _fail_if_called(**kwargs: Any) -> None:
        """
        Marker for ``run_claude_native`` — fails the test if reached.

        :param kwargs: Wrapper kwargs (ignored).
        """
        del kwargs
        invoked.append("run_claude_native")

    monkeypatch.setattr(
        "omnigent.claude_native.run_claude_native",
        _fail_if_called,
    )

    resume_dispatch.run_resume(
        target=None,
        server="https://example.com",
    )
    # If the wrapper was invoked we'd see "run_claude_native" here —
    # which would be the silent-fresh-session bug.
    assert invoked == []


# ── _dispatch_by_runtime — id-known dispatch ──────────────


def test_dispatch_by_runtime_claude_native_remote_routes_to_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Remote claude-native conv ⇒ ``run_claude_native(server=..., session_id=conv_id)``.

    The Omnigent conv id MUST be preserved as ``session_id`` (the
    wrapper's resume kwarg). A bug that passed ``None`` would mint a
    fresh session and the user would lose their prior context.
    Also asserts ``server`` carries through so the wrapper hits the
    right Omnigent server.
    """
    monkeypatch.setattr(
        resume_dispatch,
        "_read_wrapper_label_remote",
        lambda *, server, conv_id: "claude-code-native-ui",
    )
    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> None:
        """
        Record the kwargs ``run_claude_native`` was called with.

        :param kwargs: Wrapper kwargs.
        """
        captured.update(kwargs)

    monkeypatch.setattr("omnigent.claude_native.run_claude_native", _capture)

    resume_dispatch._dispatch_by_runtime(
        target="conv_abc",
        server="https://example.com/",  # trailing slash — must be normalized
    )

    # session_id preserves the Omnigent conv id end-to-end.
    assert captured["session_id"] == "conv_abc"
    # Trailing slash stripped — the wrapper expects a bare base URL.
    assert captured["server"] == "https://example.com"
    # No leaking claude args; the wrapper builds its own.
    assert captured["claude_args"] == ()


def test_dispatch_by_runtime_codex_native_remote_routes_to_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Remote codex-native conv ⇒ ``run_codex_native(server=..., session_id=conv_id)``.

    The Omnigent conv id must be preserved exactly like the
    claude-native path, but the runtime-specific passthrough kwarg is
    ``codex_args``.
    """
    monkeypatch.setattr(
        resume_dispatch,
        "_read_wrapper_label_remote",
        lambda *, server, conv_id: "codex-native-ui",
    )
    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> None:
        """
        Record the kwargs ``run_codex_native`` was called with.

        :param kwargs: Wrapper kwargs.
        """
        captured.update(kwargs)

    monkeypatch.setattr("omnigent.codex_native.run_codex_native", _capture)

    resume_dispatch._dispatch_by_runtime(
        target="conv_abc",
        server="https://example.com/",
    )

    assert captured["session_id"] == "conv_abc"
    assert captured["server"] == "https://example.com"
    assert captured["codex_args"] == ()


def test_dispatch_by_runtime_codex_native_local_routes_to_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Local codex-native conv routes to ``run_codex_native``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(
        resume_dispatch,
        "_read_wrapper_label_local",
        lambda *, conv_id: "codex-native-ui",
    )
    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> None:
        """
        Record the kwargs ``run_codex_native`` was called with.

        :param kwargs: Wrapper kwargs.
        :returns: None.
        """
        captured.update(kwargs)

    monkeypatch.setattr("omnigent.codex_native.run_codex_native", _capture)

    resume_dispatch._dispatch_by_runtime(
        target="conv_codex",
        server=None,
    )

    assert captured["session_id"] == "conv_codex"
    assert captured["server"] is None
    assert captured["codex_args"] == ()


def test_dispatch_by_runtime_antigravity_native_remote_routes_to_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Remote antigravity-native conv ⇒ ``run_antigravity_native(server=..., session_id=...)``.

    The Omnigent conv id must be preserved exactly like the codex/claude
    paths, but the runtime-specific passthrough kwarg is
    ``antigravity_args``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(
        resume_dispatch,
        "_read_wrapper_label_remote",
        lambda *, server, conv_id: "antigravity-native-ui",
    )
    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> None:
        """
        Record the kwargs ``run_antigravity_native`` was called with.

        :param kwargs: Wrapper kwargs.
        :returns: None.
        """
        captured.update(kwargs)

    monkeypatch.setattr("omnigent.antigravity_native.run_antigravity_native", _capture)

    resume_dispatch._dispatch_by_runtime(
        target="conv_agy",
        server="https://example.com/",
    )

    assert captured["session_id"] == "conv_agy"
    assert captured["server"] == "https://example.com"
    assert captured["antigravity_args"] == ()


def test_dispatch_by_runtime_antigravity_native_local_routes_to_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Local antigravity-native conv routes to ``run_antigravity_native``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(
        resume_dispatch,
        "_read_wrapper_label_local",
        lambda *, conv_id: "antigravity-native-ui",
    )
    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> None:
        """
        Record the kwargs ``run_antigravity_native`` was called with.

        :param kwargs: Wrapper kwargs.
        :returns: None.
        """
        captured.update(kwargs)

    monkeypatch.setattr("omnigent.antigravity_native.run_antigravity_native", _capture)

    resume_dispatch._dispatch_by_runtime(
        target="conv_agy_local",
        server=None,
    )

    assert captured["session_id"] == "conv_agy_local"
    assert captured["server"] is None
    assert captured["antigravity_args"] == ()


def test_dispatch_by_runtime_claude_native_local_still_routes_to_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Local claude-native dispatch remains routed to ``run_claude_native``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(
        resume_dispatch,
        "_read_wrapper_label_local",
        lambda *, conv_id: "claude-code-native-ui",
    )
    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> None:
        """
        Record the kwargs ``run_claude_native`` was called with.

        :param kwargs: Wrapper kwargs.
        :returns: None.
        """
        captured.update(kwargs)

    monkeypatch.setattr("omnigent.claude_native.run_claude_native", _capture)

    resume_dispatch._dispatch_by_runtime(
        target="conv_claude",
        server=None,
    )

    assert captured["session_id"] == "conv_claude"
    assert captured["server"] is None
    assert captured["claude_args"] == ()


def test_dispatch_by_runtime_non_wrapper_local_raises_with_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Local non-wrapper conv surfaces the ``omnigent run --resume`` hint.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(
        resume_dispatch,
        "_read_wrapper_label_local",
        lambda *, conv_id: None,
    )

    with pytest.raises(click.ClickException) as excinfo:
        resume_dispatch._dispatch_by_runtime(
            target="conv_chat",
            server=None,
        )

    msg = excinfo.value.message
    assert "conv_chat" in msg
    assert "omnigent run --resume" in msg
    assert "<agent.yaml>" in msg


def test_read_wrapper_label_local_reads_persistent_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Local dispatch classifies sessions from ``~/.omnigent/chat.db``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary persistent Omnigent directory.
    :returns: None.
    """
    import omnigent.chat as chat_mod
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    db_path = tmp_path / "chat.db"
    store = SqlAlchemyConversationStore(f"sqlite:///{db_path}")
    created = store.create_session_with_agent(
        agent_id="ag_codex",
        agent_name="codex-native-ui",
        agent_bundle_location="ag_codex/bundle",
        agent_description=None,
        labels={"omnigent.wrapper": "codex-native-ui"},
    )
    monkeypatch.setattr(chat_mod, "_omnigent_persistent_dir", lambda: tmp_path)

    result = resume_dispatch._read_wrapper_label_local(conv_id=created.conversation.id)

    assert result == "codex-native-ui"


def test_dispatch_by_runtime_non_claude_native_remote_raises_with_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Remote non-claude-native conv ⇒ ``ClickException`` with a
    copy-pasteable ``omnigent run --resume`` hint.

    The hint MUST include both the conv id and the original
    ``--server`` URL so the user's next attempt works without
    them having to remember additional flags. A regression that
    surfaced a generic "wrong runtime" error would leave the
    user stuck.
    """
    monkeypatch.setattr(
        resume_dispatch,
        "_read_wrapper_label_remote",
        lambda *, server, conv_id: None,  # no wrapper label
    )

    def _fail_if_called(**kwargs: Any) -> None:
        """Marker — fails the test if ``run_claude_native`` is called."""
        del kwargs
        raise AssertionError("run_claude_native invoked on non-claude conv")

    monkeypatch.setattr("omnigent.claude_native.run_claude_native", _fail_if_called)

    with pytest.raises(click.ClickException) as excinfo:
        resume_dispatch._dispatch_by_runtime(
            target="conv_xyz",
            server="https://example.com",
        )
    msg = excinfo.value.message
    # All three load-bearing pieces of the hint must appear.
    assert "conv_xyz" in msg
    assert "omnigent run --resume" in msg
    assert "https://example.com" in msg


# ── _read_wrapper_label_remote ────────────────────────────


def test_read_wrapper_label_remote_returns_label_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Happy path: 200 response with the wrapper label set returns the
    label value, which the caller compares against the claude-native
    sentinel.
    """

    def _fake_get(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        """
        Return a canned ``GET /v1/sessions/{id}`` response.

        :param url: Request URL (used to validate path shape).
        :param headers: Auth headers (ignored).
        :param timeout: Request timeout (ignored).
        :returns: A 200 response with a labelled body.
        """
        del headers, timeout
        assert url.endswith("/v1/sessions/conv_abc"), url
        return httpx.Response(
            200,
            json={
                "id": "conv_abc",
                "agent_id": "ag_1",
                "status": "idle",
                "created_at": 1,
                "labels": {"omnigent.wrapper": "claude-code-native-ui"},
            },
        )

    monkeypatch.setattr(httpx, "get", _fake_get)
    monkeypatch.setattr(
        "omnigent.chat._remote_headers",
        lambda *, server_url: {},
    )

    result = resume_dispatch._read_wrapper_label_remote(
        server="https://example.com",
        conv_id="conv_abc",
    )
    assert result == "claude-code-native-ui"


def test_read_wrapper_label_remote_returns_none_when_label_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A conv with no ``omnigent.wrapper`` label returns ``None``, which
    the caller treats as "not claude-native" (the right call — wrappers
    stamp their label on every session they own; absence means a
    different runtime).
    """

    def _fake_get(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        """Return a 200 with no wrapper label."""
        del url, headers, timeout
        return httpx.Response(
            200,
            json={
                "id": "conv_abc",
                "agent_id": "ag_1",
                "status": "idle",
                "created_at": 1,
                "labels": {"some.other": "label"},
            },
        )

    monkeypatch.setattr(httpx, "get", _fake_get)
    monkeypatch.setattr(
        "omnigent.chat._remote_headers",
        lambda *, server_url: {},
    )

    result = resume_dispatch._read_wrapper_label_remote(
        server="https://example.com",
        conv_id="conv_abc",
    )
    assert result is None


def test_read_wrapper_label_remote_raises_on_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    404 means the conv id doesn't exist — surface a clear error with
    the conv id and server so the user can fix a typo or check the
    server. Without this, the caller would proceed with a None label
    and surface the generic "not claude-native" hint, which would
    misdirect the user.
    """

    def _fake_get(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        """Return a 404."""
        del url, headers, timeout
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    monkeypatch.setattr(httpx, "get", _fake_get)
    monkeypatch.setattr(
        "omnigent.chat._remote_headers",
        lambda *, server_url: {},
    )

    with pytest.raises(click.ClickException) as excinfo:
        resume_dispatch._read_wrapper_label_remote(
            server="https://example.com",
            conv_id="conv_missing",
        )
    assert "conv_missing" in excinfo.value.message
    assert "not found" in excinfo.value.message
