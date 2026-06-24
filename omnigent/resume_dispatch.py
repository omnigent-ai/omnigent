"""Top-level ``omnigent resume`` dispatch.

Glue layer that converts the user's "take me back to where I was"
intent into the right wrapper invocation. Lives outside ``cli.py`` so
the dispatch logic (HTTP lookup, picker, claude-native handoff) is
testable without spinning up a Click runner, and so ``cli.py`` does
not need to import the claude-native wrapper at every CLI startup.

The dispatch contract:

- ``omnigent resume <conv_id>`` — fetch the conversation, read its
  ``omnigent.wrapper`` label, and dispatch by runtime.
- ``omnigent resume`` (no id) — open the cross-agent picker (see
  :func:`omnigent.repl._resume_picker.pick_conversation_cross_agent_from_sdk`)
  and dispatch the selection the same way.

Terminal-native conversations are dispatched in-process today.
Everything else surfaces a copy-pasteable hint to the existing
``omnigent run --resume`` invocation — the agentless ``run
--resume`` shape is tracked separately.
"""

from __future__ import annotations

import asyncio
import logging

import click
import httpx

from omnigent._wrapper_labels import (
    WRAPPER_LABEL_KEY as _WRAPPER_LABEL_KEY,
)
from omnigent.native_coding_agents import native_coding_agent_for_wrapper_label

_logger = logging.getLogger(__name__)


def run_resume(
    *,
    target: str | None,
    server: str | None,
) -> None:
    """
    Resolve the user's resume request and dispatch by runtime.

    Direct-id form (``target`` provided) is the common CUJ — the user
    knows the conversation they want and types its id; we hit the
    server once for the wrapper label and route to the right wrapper.
    Picker form (``target`` is None) requires ``--server`` because
    starting an empty local server just to host the picker would
    leak state and collide with any other in-flight ``omnigent``
    process the user has running.

    :param target: Optional conversation id, e.g. ``"conv_abc123"``.
        ``None`` selects the picker form.
    :param server: Optional remote Omnigent server URL. Required in the
        picker form (no agent is supplied so we can't bootstrap a
        local server). For the direct-id form, ``None`` reads the
        persistent local session store and dispatches to the matching
        native wrapper.
    :returns: None when the dispatched wrapper exits.
    :raises click.UsageError: When the picker form is invoked
        without ``--server`` (the user-facing message names both
        recoverable paths).
    :raises click.ClickException: When the conversation cannot be
        resolved or the dispatched wrapper raises.
    """
    if target is None:
        if server is None:
            raise click.UsageError(
                "`omnigent resume` (no id) requires `--server <url>`. "
                "Pass a conversation id (`omnigent resume conv_...`) "
                "to use the persistent local store.",
            )
        target = _pick_conversation_for_resume(server=server)
        if target is None:
            # Picker cancelled (or no prior conversations on this
            # server). Treat as a clean exit — the user explicitly
            # chose not to pick anything.
            return

    _dispatch_by_runtime(
        target=target,
        server=server,
    )


def _pick_conversation_for_resume(
    *,
    server: str,
) -> str | None:
    """
    Run the cross-agent picker against *server* and return the choice.

    Wires the SDK client up just for the picker call; closes it on
    exit so a long picker hold doesn't keep the connection open.
    Cancellation (user hits ``q`` / Enter on an empty line) and
    empty-list both surface as ``None`` — the caller treats both as
    "no resume requested" and exits cleanly.

    :param server: Remote Omnigent server URL, e.g.
        ``"https://example.databricksapps.com"``.
    :returns: Selected conversation id, or ``None`` on cancel.
    """
    from omnigent.chat import _remote_headers
    from omnigent.repl._resume_picker import pick_conversation_cross_agent_from_sdk

    base_url = server.rstrip("/")
    headers = _remote_headers(server_url=base_url)

    async def _drive() -> str | None:
        """
        Open one SDK client, run the picker, close on exit.

        :returns: Selected conversation id, ``None`` on cancel /
            empty list.
        """
        # Deferred import: ``omnigent_client`` carries the full SDK
        # surface and is only needed for the picker, not for every
        # ``omnigent`` invocation.
        from omnigent_client import OmnigentClient

        async with OmnigentClient(base_url=base_url, headers=headers) as client:
            return await pick_conversation_cross_agent_from_sdk(client)

    try:
        return asyncio.run(_drive())
    except click.ClickException:
        raise
    except httpx.HTTPError as exc:
        # Network failure reaching ``server``. Surface a clear error
        # rather than a raw httpx traceback — the user can act on
        # "couldn't reach <url>" but not on a stack frame.
        raise click.ClickException(
            f"Failed to load conversations from {base_url!r}: {exc}",
        ) from exc
    except Exception as exc:
        # Catch-all to translate any other SDK/transport failure into
        # a user-actionable error. Re-raises ``ClickException`` so
        # other Click handlers don't double-wrap.
        raise click.ClickException(
            f"Picker failed against {base_url!r}: {type(exc).__name__}: {exc}",
        ) from exc


def _dispatch_by_runtime(
    *,
    target: str,
    server: str | None,
) -> None:
    """
    Fetch *target*'s wrapper label and dispatch to the matching runtime.

    Terminal-native sessions route into their wrapper entry point
    carrying ``--server`` through. Non-wrapper
    conversations surface a clear ``ClickException`` pointing at
    the existing ``omnigent run --resume`` invocation — the
    agentless ``run --resume`` (drops the ``AGENT`` requirement
    via server-side agent lookup) is tracked separately and not in
    this PR's scope.

    :param target: Conversation id, e.g. ``"conv_abc123"``.
    :param server: Optional remote Omnigent server URL. ``None`` when
        the lookup should hit a freshly-started local server (the
        claude-native wrapper owns its own local server lifecycle).
    :raises click.ClickException: When the conversation can't be
        resolved, can't be classified, or is not a terminal-native
        session.
    """
    if server is not None:
        wrapper = _read_wrapper_label_remote(server=server, conv_id=target)
        if _dispatch_wrapper(
            wrapper=wrapper,
            server=server.rstrip("/"),
            session_id=target,
        ):
            return
        raise click.ClickException(
            f"Conversation {target!r} is not a terminal-native session "
            f"(wrapper={wrapper!r}). To resume it, run "
            f"`omnigent run --resume {target} <agent.yaml> --server "
            f"{server}`. The agentless form is tracked separately.",
        )

    wrapper = _read_wrapper_label_local(conv_id=target)
    if _dispatch_wrapper(
        wrapper=wrapper,
        server=None,
        session_id=target,
    ):
        return
    raise click.ClickException(
        f"Conversation {target!r} is not a terminal-native session "
        f"(wrapper={wrapper!r}). To resume it, run "
        f"`omnigent run --resume {target} <agent.yaml>`. "
        "The agentless form is tracked separately.",
    )


def _dispatch_wrapper(
    *,
    wrapper: str | None,
    server: str | None,
    session_id: str,
) -> bool:
    """
    Dispatch a terminal-native wrapper session.

    :param wrapper: Value from ``labels.omnigent.wrapper``.
    :param server: Omnigent server base URL without trailing slash, or
        ``None`` for the local persistent server path.
    :param session_id: Omnigent conversation id.
    :returns: ``True`` when a wrapper handled the session.
    """
    native_agent = native_coding_agent_for_wrapper_label(wrapper)
    if native_agent is None:
        return False
    if native_agent.key == "claude":
        from omnigent.claude_native import run_claude_native

        run_claude_native(
            server=server,
            session_id=session_id,
            claude_args=(),
        )
        return True
    if native_agent.key == "codex":
        from omnigent.codex_native import run_codex_native

        run_codex_native(
            server=server,
            session_id=session_id,
            codex_args=(),
        )
        return True
    if native_agent.key == "pi":
        from omnigent.pi_native import run_pi_native

        run_pi_native(
            server=server,
            session_id=session_id,
            pi_args=(),
        )
        return True
    if native_agent.key == "cursor":
        from omnigent.cursor_native import run_cursor_native

        run_cursor_native(
            server=server,
            session_id=session_id,
            cursor_args=(),
        )
        return True
    if native_agent.key == "goose":
        from omnigent.goose_native import run_goose_native

        run_goose_native(
            server=server,
            session_id=session_id,
            goose_args=(),
        )
        return True
    if native_agent.key == "antigravity":
        from omnigent.antigravity_native import run_antigravity_native

        run_antigravity_native(
            server=server,
            session_id=session_id,
            antigravity_args=(),
        )
        return True
    if native_agent.key == "qwen":
        from omnigent.qwen_native import run_qwen_native

        run_qwen_native(
            server=server,
            session_id=session_id,
            qwen_args=(),
        )
        return True
    if native_agent.key == "kimi":
        from omnigent.kimi_native import run_kimi_native

        run_kimi_native(
            server=server,
            session_id=session_id,
            kimi_args=(),
        )
        return True
    return False


def _read_wrapper_label_local(*, conv_id: str) -> str | None:
    """
    Read a conversation's wrapper label from the local persistent store.

    :param conv_id: Local Omnigent conversation id, e.g. ``"conv_abc123"``.
    :returns: Value of ``labels.omnigent.wrapper``, or ``None`` when
        no wrapper label is present.
    :raises click.ClickException: If the conversation id is not found
        in the local persistent store.
    """
    from omnigent.chat import _omnigent_persistent_dir
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    db_path = _omnigent_persistent_dir() / "chat.db"
    store = SqlAlchemyConversationStore(f"sqlite:///{db_path}")
    conversation = store.get_conversation(conv_id)
    if conversation is None:
        raise click.ClickException(
            f"Conversation {conv_id!r} not found in the local persistent store. "
            "Pass --server if the conversation lives on a remote Omnigent server.",
        )
    labels = conversation.labels
    return labels.get(_WRAPPER_LABEL_KEY) if isinstance(labels, dict) else None


def _read_wrapper_label_remote(
    *,
    server: str,
    conv_id: str,
) -> str | None:
    """
    GET the conversation on *server* and return its wrapper label.

    Used only on the remote-server path. The local-server path
    delegates the label check to :func:`run_claude_native`'s
    cold-resume branch (which already issues a GET and surfaces a
    clear error on mismatch), so duplicating it here would mean two
    GETs for the same resolution.

    :param server: Remote Omnigent server URL.
    :param conv_id: Omnigent conversation id.
    :returns: Wrapper label string, or ``None`` when no
        ``omnigent.wrapper`` label is present on the row.
    :raises click.ClickException: On 404 (conv not found), other
        non-200 status, or a non-JSON / non-object response body.
    """
    from omnigent.chat import _remote_headers

    base_url = server.rstrip("/")
    headers = _remote_headers(server_url=base_url)
    try:
        resp = httpx.get(
            f"{base_url}/v1/sessions/{conv_id}",
            headers=headers,
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise click.ClickException(
            f"Failed to reach {base_url!r}: {exc}",
        ) from exc
    if resp.status_code == 404:
        raise click.ClickException(
            f"Conversation {conv_id!r} not found on {base_url!r}.",
        )
    if resp.status_code != 200:
        raise click.ClickException(
            f"Failed to fetch conversation {conv_id!r} ({resp.status_code}): {resp.text[:400]}",
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise click.ClickException(
            f"Conversation fetch returned non-JSON body: {exc}",
        ) from exc
    if not isinstance(body, dict):
        raise click.ClickException("Conversation fetch returned a non-object body.")
    labels = body.get("labels")
    if not isinstance(labels, dict):
        return None
    value = labels.get(_WRAPPER_LABEL_KEY)
    return value if isinstance(value, str) else None
