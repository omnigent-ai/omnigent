"""The viewer's local timezone, remembered per session.

A web client reports its IANA zone on each user message (``client_timezone``
on ``POST /v1/sessions/{id}/events``); the server validates it and the runner
keeps the latest value per session. The session's tools and prompt then speak
in the user's wall clock: ``sys_scheduled_task_create`` defaults an omitted
schedule zone to it, and the composed instructions name it. Sessions whose
clients never report a zone (CLI, SDK) keep the UTC defaults.
"""

from __future__ import annotations

from omnigent.util.timezones import is_valid_timezone

_session_client_timezones: dict[str, str] = {}


def remember_client_timezone(conversation_id: str, timezone: object) -> None:
    """
    Record the zone a client reported for *conversation_id*.

    :param conversation_id: Session/conversation id.
    :param timezone: The reported value; anything but a valid IANA key is ignored.
    """
    if is_valid_timezone(timezone):
        _session_client_timezones[conversation_id] = str(timezone)


def client_timezone_for(conversation_id: str | None) -> str | None:
    """
    Return the zone most recently reported for *conversation_id*.

    :param conversation_id: Session/conversation id, or ``None`` when unknown.
    :returns: An IANA zone key, or ``None`` when no client reported one.
    """
    if conversation_id is None:
        return None
    return _session_client_timezones.get(conversation_id)


def forget_client_timezone(conversation_id: str) -> None:
    """
    Drop the remembered zone when a session is torn down.

    :param conversation_id: Session/conversation id.
    """
    _session_client_timezones.pop(conversation_id, None)
