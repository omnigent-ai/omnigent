"""Resume-picker server failures must surface as actionable CLI errors.

``_resolve_session_id_for_resume`` drives the resume picker against the
server's session listing. A gateway 5xx (upstream app down / restarting)
or a transport failure there is a transient server-side condition, so it
must raise ``click.ClickException`` with retry guidance instead of
letting the raw client error escape to the crash handler.
"""

from __future__ import annotations

import re

import click
import httpx
import pytest
from omnigent_client._errors import OmnigentError, ServerError

from omnigent import claude_native
from omnigent.repl import _resume_picker

_BASE_URL = "http://server.example"


def _run_picker() -> str | None:
    """Invoke the resolver on the bare ``--resume`` (picker) path."""
    return claude_native._resolve_session_id_for_resume(
        base_url=_BASE_URL,
        headers={},
        session_id=None,
        resume_picker=True,
    )


def test_gateway_5xx_on_session_listing_raises_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 502 from the picker's session listing becomes a ClickException.

    The message must name the server URL, echo the server's error, and
    point at retrying — not dump the raw ServerError to the crash handler.
    """

    async def _boom(*args: object, **kwargs: object) -> str | None:
        raise ServerError("Bad Gateway", 502, "")

    monkeypatch.setattr(_resume_picker, "pick_conversation_by_wrapper_label_from_sdk", _boom)

    with pytest.raises(
        click.ClickException,
        match=re.escape(f"at {_BASE_URL}: Bad Gateway (HTTP 502)"),
    ) as exc_info:
        _run_picker()

    assert "retry" in str(exc_info.value).lower()


def test_auth_4xx_on_session_listing_is_not_framed_as_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 4xx (e.g. expired auth) gets credential guidance, not retry advice."""

    async def _boom(*args: object, **kwargs: object) -> str | None:
        raise OmnigentError("Unauthorized", 401, "")

    monkeypatch.setattr(_resume_picker, "pick_conversation_by_wrapper_label_from_sdk", _boom)

    with pytest.raises(
        click.ClickException,
        match=re.escape(f"at {_BASE_URL}: Unauthorized (HTTP 401)"),
    ) as exc_info:
        _run_picker()

    message = str(exc_info.value)
    assert "credentials" in message.lower()
    assert "transient" not in message.lower()


def test_transport_failure_on_session_listing_raises_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport-level failure (e.g. read timeout) is handled the same way."""

    async def _boom(*args: object, **kwargs: object) -> str | None:
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(_resume_picker, "pick_conversation_by_wrapper_label_from_sdk", _boom)

    with pytest.raises(
        click.ClickException,
        match=re.escape(f"Could not reach the omnigent server at {_BASE_URL}"),
    ):
        _run_picker()


def test_explicit_session_id_bypasses_picker_and_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--resume <id>`` never contacts the server, so nothing can 502."""

    async def _boom(*args: object, **kwargs: object) -> str | None:
        raise AssertionError("picker must not run when an id is given")

    monkeypatch.setattr(_resume_picker, "pick_conversation_by_wrapper_label_from_sdk", _boom)

    resolved = claude_native._resolve_session_id_for_resume(
        base_url=_BASE_URL,
        headers={},
        session_id="conv_123",
        resume_picker=True,
    )
    assert resolved == "conv_123"


def test_picker_cancel_still_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """The graceful-error wrapping must not break the cancel path."""

    async def _cancelled(*args: object, **kwargs: object) -> str | None:
        return None

    monkeypatch.setattr(_resume_picker, "pick_conversation_by_wrapper_label_from_sdk", _cancelled)

    assert _run_picker() is None
