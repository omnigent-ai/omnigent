"""Cursor-native tool-approval mirror (TUI → web elicitation).

The ``cursor-agent`` TUI gates its own tool calls (shell, file write, …) with an
in-terminal approval prompt that lives only in cursor's in-memory state — there
is no public event or hook for it, and it is not written to cursor's transcript
or chat store while pending. To surface those approvals in the Omnigent web UI
(so a user can answer from the chat view, not just inside the embedded
terminal), the runner watches the cursor pane:

1. poll ``capture-pane`` and detect an approval block (the bordered
   "Run this command? … (y) … (esc or n)" widget),
2. POST it to the server's ``cursor-permission-request`` hook, which publishes
   the standard ``response.elicitation_request`` event and parks for the web
   verdict (the same machinery codex-native uses),
3. on the verdict, drive the cursor TUI by sending the advertised keystroke
   (``y`` to approve, ``Escape`` to reject),
4. if the prompt instead disappears on its own (the user answered inside the
   embedded terminal), POST ``external_elicitation_resolved`` so the parked web
   card clears.

This deliberately does NOT modify cursor's JS bundle and does NOT suppress
cursor's native gate; cursor's own prompt remains the source of truth and the
fallback if pane detection ever fails (the user can still answer in the
terminal). See ``docs/cursor-native-tui-mirror-plan.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from omnigent.cursor_native_bridge import capture_cursor_pane, send_cursor_pane_keys

_logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.3
# The approval hook parks server-side until a human answers; allow a day, well
# past any realistic wait, so the runner's POST never abandons a live prompt.
_POST_TIMEOUT_S = 86400.0

# Markers used to recognise an approval block in the rendered pane. Cursor's
# per-tool prompt advertises keys in PARENTHESES ("(y)", "(esc or n)"); the
# first-run Workspace-Trust modal uses BRACKETS ("[a] Trust this workspace"),
# so keying on parenthesised options cleanly excludes the trust modal.
_ACCEPT_VERB_RE = re.compile(r"\b(run|allow|approve|apply|accept|yes)\b", re.IGNORECASE)
_DECLINE_VERB_RE = re.compile(r"\b(skip|reject|deny|cancel|no)\b", re.IGNORECASE)
_PAREN_KEY_RE = re.compile(r"\(([^)]*)\)")
_TITLE_SCAN_LINES = 12


@dataclass(frozen=True)
class CursorApprovalPrompt:
    """A parsed cursor-agent approval prompt.

    :param title: The prompt's question line, e.g. ``"Run this command?"``.
    :param subject: The operation detail, e.g. ``"echo hi > out.txt"`` (the
        ``$``-prefixed command for shell ops), or ``""``.
    :param operation_type: Coarse type, ``"shell"`` when a command subject was
        found, else ``"tool"``.
    :param message: Human-readable card message.
    :param preview: Compact preview for the card (the command/subject).
    :param accept_key: tmux key to approve, e.g. ``"y"``.
    :param decline_key: tmux key to reject, e.g. ``"Escape"``.
    :param block_hash: Stable hash of (title, subject) used to dedupe a prompt
        across polls and to mint a stable elicitation id.
    """

    title: str
    subject: str
    operation_type: str
    message: str
    preview: str
    accept_key: str
    decline_key: str
    block_hash: str


def cursor_permission_elicitation_id(session_id: str, block_hash: str) -> str:
    """Return the deterministic Omnigent elicitation id for a cursor prompt."""
    return f"elicit_cursor_{session_id}_{block_hash}"


def _paren_key(line: str) -> str | None:
    """Extract a single-letter key from the last ``(…)`` group on *line*."""
    groups = _PAREN_KEY_RE.findall(line)
    if not groups:
        return None
    candidate = groups[-1].strip().lower()
    return candidate if re.fullmatch(r"[a-z]", candidate) else None


def _clean_subject(line: str) -> str:
    """Normalise a ``$``-prefixed command line into a bare command string."""
    text = line.strip().lstrip("$").strip()
    text = text.replace("Waiting for approval...", "").strip()
    # Strip a trailing " in <cwd>" hint that cursor appends to the command.
    text = re.sub(r"\s+in\s+\S+$", "", text).strip()
    return text[:1024]


def parse_cursor_approval_prompt(pane: str) -> CursorApprovalPrompt | None:
    """
    Parse a cursor-agent approval block out of rendered pane text.

    Returns ``None`` when no approval prompt is visible (including the
    Workspace-Trust modal, which uses ``[key]`` brackets rather than the
    ``(key)`` parentheses of a tool-approval prompt). Requires BOTH a
    parenthesised accept option and a decline option to avoid false positives.

    :param pane: Visible pane text from ``capture-pane -p``.
    :returns: The parsed prompt, or ``None``.
    """
    if not pane:
        return None
    lines = pane.splitlines()

    accept_idx: int | None = None
    for i, line in enumerate(lines):
        if "(y)" not in line.lower():
            continue
        if _ACCEPT_VERB_RE.search(line) and "everything" not in line.lower():
            accept_idx = i
            break
    if accept_idx is None:
        return None

    decline_key: str | None = None
    for line in lines[accept_idx : accept_idx + 5]:
        low = line.lower()
        if "(esc" in low or "(n)" in low or _DECLINE_VERB_RE.search(line):
            decline_key = "Escape" if "esc" in low else (_paren_key(line) or "Escape")
            break
    if decline_key is None:
        return None

    accept_key = _paren_key(lines[accept_idx]) or "y"

    title = ""
    subject = ""
    start = max(0, accept_idx - _TITLE_SCAN_LINES)
    for line in reversed(lines[start:accept_idx]):
        stripped = line.strip()
        if not subject and stripped.startswith("$"):
            subject = _clean_subject(stripped)
        if not title and stripped.endswith("?"):
            title = stripped
    if not title and not subject:
        return None

    operation_type = "shell" if subject else "tool"
    message = title or "Cursor wants approval to run a tool"
    preview = subject or title
    block_hash = hashlib.sha256(f"{title}\n{subject}".encode()).hexdigest()[:16]
    return CursorApprovalPrompt(
        title=title,
        subject=subject,
        operation_type=operation_type,
        message=message,
        preview=preview,
        accept_key=accept_key,
        decline_key=decline_key,
        block_hash=block_hash,
    )


async def supervise_cursor_approval_mirror(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    auth: httpx.Auth | None = None,
    poll_interval_s: float = _POLL_INTERVAL_S,
) -> None:
    """
    Poll the cursor pane and mirror its approval prompts to web elicitations.

    Runs for the session's lifetime (cancelled on teardown). At most one prompt
    is active at a time (cursor shows them sequentially): a new block spawns a
    task that parks on the server and, on the web verdict, sends the keystroke;
    a block that vanishes while still parked means the user answered in the TUI,
    so the parked card is released via ``external_elicitation_resolved``.

    :param base_url: Server base URL.
    :param headers: Auth/routing headers for the runner's requests.
    :param session_id: Omnigent conversation id.
    :param bridge_dir: The cursor-native bridge dir holding ``tmux.json``.
    :param auth: Optional httpx auth for the runner's requests.
    :param poll_interval_s: Pane poll cadence in seconds.
    """
    active: dict[str, object] | None = None
    timeout = httpx.Timeout(_POST_TIMEOUT_S, connect=10.0)
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        while True:
            try:
                pane = await asyncio.to_thread(capture_cursor_pane, bridge_dir)
                prompt = parse_cursor_approval_prompt(pane) if pane else None
                if prompt is not None:
                    if active is None or active["key"] != prompt.block_hash:
                        elicitation_id = cursor_permission_elicitation_id(
                            session_id, prompt.block_hash
                        )
                        task = asyncio.create_task(
                            _run_one_approval(
                                client,
                                session_id=session_id,
                                bridge_dir=bridge_dir,
                                prompt=prompt,
                                elicitation_id=elicitation_id,
                            ),
                            name=f"cursor-approval-{prompt.block_hash}",
                        )
                        active = {
                            "key": prompt.block_hash,
                            "elicitation_id": elicitation_id,
                            "task": task,
                        }
                elif active is not None:
                    task = active["task"]
                    if isinstance(task, asyncio.Task) and not task.done():
                        # Vanished while still parked → answered in the TUI;
                        # release the web card.
                        await _post_external_elicitation_resolved(
                            client, session_id, str(active["elicitation_id"])
                        )
                    active = None
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "cursor approval mirror poll failed; session=%s bridge_dir=%s",
                    session_id,
                    bridge_dir,
                )
            await asyncio.sleep(poll_interval_s)


async def _run_one_approval(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    prompt: CursorApprovalPrompt,
    elicitation_id: str,
) -> None:
    """Park one cursor prompt on the server and send the verdict keystroke."""
    payload = {
        "elicitation_id": elicitation_id,
        "operation_type": prompt.operation_type,
        "message": prompt.message,
        "content_preview": prompt.preview,
    }
    try:
        response = await client.post(
            f"/v1/sessions/{session_id}/hooks/cursor-permission-request",
            json=payload,
        )
    except httpx.HTTPError:
        _logger.exception("cursor permission hook POST failed; session=%s", session_id)
        return
    if response.status_code >= 400:
        _logger.warning(
            "cursor permission hook rejected: status=%s body=%s",
            response.status_code,
            response.text[:512],
        )
        return
    if not response.content:
        # Empty 2xx → resolved elsewhere (TUI answered) or timeout: no keystroke.
        return
    try:
        result = response.json()
    except ValueError:
        _logger.warning("cursor permission hook returned non-JSON: %s", response.text[:512])
        return
    action = result.get("action") if isinstance(result, dict) else None
    key = None
    if action == "accept":
        key = prompt.accept_key
    elif action in {"decline", "cancel"}:
        key = prompt.decline_key
    if key is None:
        return
    try:
        await asyncio.to_thread(send_cursor_pane_keys, bridge_dir, key)
    except RuntimeError:
        _logger.exception(
            "failed to send cursor approval keystroke %r; session=%s", key, session_id
        )


async def _post_external_elicitation_resolved(
    client: httpx.AsyncClient, session_id: str, elicitation_id: str
) -> None:
    """Tell the server the native TUI answered a pending cursor prompt."""
    try:
        response = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "external_elicitation_resolved",
                "data": {"elicitation_id": elicitation_id},
            },
            timeout=10.0,
        )
        if response.status_code >= 400:
            _logger.warning(
                "cursor external_elicitation_resolved rejected: status=%s body=%s",
                response.status_code,
                response.text[:512],
            )
    except httpx.HTTPError:
        _logger.exception("cursor external_elicitation_resolved POST failed")
