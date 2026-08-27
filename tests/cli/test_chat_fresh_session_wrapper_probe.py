"""The native-wrapper redirect probe must not run on a brand-new session.

``omnigent run`` creates the session, then hands it to ``_chat_with_server``
as the resume target. That made the wrapper-label probe — a full
``GET /v1/sessions/{id}`` whose only job is to spot a claude-native /
codex-native session and re-dispatch — fire on every fresh start, where it
can only ever answer "no wrapper". On a hosted server that read measured
155-228ms, spent in the last stretch before the REPL paints.
"""

from __future__ import annotations

import pytest

from omnigent import chat as chat_module


class _StopBeforeRepl(Exception):
    """Raised from the stubbed agent picker to end the call under test."""


@pytest.fixture(name="probe_calls")
def _probe_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """
    Record wrapper-redirect probes and stop the flow just after the gate.

    :param monkeypatch: pytest monkeypatch fixture.
    :returns: List that collects each probed conversation id.
    """
    calls: list[str] = []

    def _redirect(*, conversation_id: str, **_kw: object) -> bool:
        """Record the probe without claiming the resume."""
        calls.append(conversation_id)
        return False

    def _pick_agent(_base_url: str) -> str:
        """Stop the call under test right after the gate."""
        raise _StopBeforeRepl

    monkeypatch.setattr(chat_module, "_redirect_native_resume_if_needed", _redirect)
    monkeypatch.setattr(chat_module, "_pick_agent", _pick_agent)
    return calls


def test_session_created_this_run_skips_the_probe(probe_calls: list[str]) -> None:
    """A session this startup created is never probed for a wrapper label."""
    with pytest.raises(_StopBeforeRepl):
        chat_module._chat_with_server(
            "http://localhost:8000",
            None,
            resume_conversation_id="conv_fresh",
            resume_created_this_run=True,
        )

    assert probe_calls == []


def test_genuine_resume_still_probes(probe_calls: list[str]) -> None:
    """A pre-existing session keeps the redirect probe."""
    with pytest.raises(_StopBeforeRepl):
        chat_module._chat_with_server(
            "http://localhost:8000",
            None,
            resume_conversation_id="conv_old",
        )

    assert probe_calls == ["conv_old"]
