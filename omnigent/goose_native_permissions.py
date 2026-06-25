"""Goose-native tool-approval mirror (TUI → web elicitation).

In ``approve`` / ``smart_approve`` mode the native ``goose session`` TUI gates
tool calls with an in-terminal ``cliclack`` selector (its
``prompt_tool_confirmation``). To also surface those approvals in the Omnigent
web UI (so a user can answer from the chat view, not only the embedded terminal),
the runner watches the Goose pane:

1. poll ``capture-pane`` and detect the confirmation block — Goose renders the
   question ``Goose would like to call the above tool, do you allow?`` (or, with
   a security message, ``Do you allow this tool call?``) followed by a cliclack
   radio list ``Allow`` / ``Always Allow`` / ``Deny`` / ``Cancel`` (verified
   against goose-cli ``session/mod.rs::prompt_tool_confirmation``),
2. POST it to the server's generic ``native-permission-request`` hook, which
   publishes ``response.elicitation_request`` and parks for the web verdict,
3. on the verdict, DRIVE the cliclack selector: ``Enter`` chooses the
   default-highlighted ``Allow``; to deny, send ``Down`` to the ``Deny`` row then
   ``Enter`` (the ``Deny`` index is 2 when ``Always Allow`` is offered, else 1),
4. if the prompt disappears on its own (answered in the embedded terminal), POST
   ``external_elicitation_resolved`` so the parked web card clears.

This does NOT suppress Goose's gate — its cliclack prompt stays the source of
truth and the fallback if pane detection ever fails (the user can still arrow +
Enter in the terminal). Mirrors :mod:`omnigent.cursor_native_permissions`; the
arrow-driven selector (vs cursor's single-key) is the fragile part and is worth
confirming against a live Goose.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from omnigent.goose_native_bridge import capture_goose_pane, send_goose_pane_keys

_logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.3
_POST_TIMEOUT_S = 86400.0

# The confirmation question — both phrasings contain "do you allow"; it is only
# present while cliclack is awaiting a choice, so it's the liveness signal.
_PROMPT_RE = re.compile(r"do you allow", re.IGNORECASE)
# cliclack box-drawing / radio prefixes to strip when reading the subject lines.
_CLICLACK_PREFIX_RE = re.compile(r"^[\s│◆◇◊○●▲▶>|*-]+")
_ITEM_LABELS = ("Always Allow", "Allow", "Deny", "Cancel")
_SUBJECT_SCAN_LINES = 8


@dataclass(frozen=True)
class GooseApprovalPrompt:
    """A parsed Goose cliclack tool-confirmation prompt.

    :param subject: The tool/command context shown above the question (best
        effort, for the card preview + dedupe).
    :param message: Human-readable card message.
    :param preview: Compact preview for the card.
    :param deny_down_count: Number of ``Down`` presses from the default-
        highlighted ``Allow`` to reach ``Deny`` (2 with "Always Allow", else 1).
    :param block_hash: Stable hash of the subject used to dedupe across polls and
        to mint a stable elicitation id.
    """

    subject: str
    message: str
    preview: str
    deny_down_count: int
    block_hash: str


def goose_permission_elicitation_id(session_id: str, token: str) -> str:
    """Return the deterministic Omnigent elicitation id for a Goose prompt.

    *token* identifies one approval episode (a per-session counter), not the
    scraped content — the rendered tool context above the cliclack widget jitters
    across polls, so hashing it spawned a duplicate card every poll.
    """
    return f"elicit_goose_{session_id}_{token}"


def _looks_like_item(line: str) -> bool:
    """Whether *line* is one of the cliclack radio item rows."""
    stripped = _CLICLACK_PREFIX_RE.sub("", line).strip()
    return any(stripped.startswith(label) for label in _ITEM_LABELS)


def parse_goose_approval_prompt(pane: str) -> GooseApprovalPrompt | None:
    """Parse a Goose cliclack tool-confirmation block from rendered pane text.

    Requires the ``do you allow`` question AND both an ``Allow`` and a ``Deny``
    radio item, so unrelated text never trips it.

    :param pane: Visible pane text from ``capture-pane -p``.
    :returns: The parsed prompt, or ``None`` when no live prompt is visible.
    """
    if not pane:
        return None
    match = _PROMPT_RE.search(pane)
    if match is None:
        return None
    lines = pane.splitlines()
    question_idx = next((i for i, ln in enumerate(lines) if _PROMPT_RE.search(ln)), None)
    if question_idx is None:
        return None

    # The radio items render after the question.
    tail = "\n".join(lines[question_idx:])
    has_allow = re.search(r"\bAllow\b", tail) is not None
    has_deny = re.search(r"\bDeny\b", tail) is not None
    if not (has_allow and has_deny):
        return None
    # "Always Allow" present → Deny is the 3rd item (2 downs); else 2nd (1 down).
    deny_down_count = 2 if re.search(r"Always Allow", tail) else 1

    # Subject = the meaningful (non-item, non-box) lines just above the question,
    # i.e. the tool-request context Goose rendered. Best effort; used for the card
    # preview and to dedupe distinct tool calls (the question text is generic).
    subject_lines: list[str] = []
    start = max(0, question_idx - _SUBJECT_SCAN_LINES)
    for ln in lines[start:question_idx]:
        if _looks_like_item(ln):
            continue
        cleaned = _CLICLACK_PREFIX_RE.sub("", ln).strip()
        if cleaned:
            subject_lines.append(cleaned)
    subject = " | ".join(subject_lines[-3:])[:1024]

    digest_src = subject or tail
    block_hash = hashlib.sha256(digest_src.encode("utf-8")).hexdigest()[:16]
    return GooseApprovalPrompt(
        subject=subject,
        message="Goose wants to call a tool. Allow?",
        preview=subject or "Goose tool call",
        deny_down_count=deny_down_count,
        block_hash=block_hash,
    )


async def supervise_goose_approval_mirror(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    auth: httpx.Auth | None = None,
    poll_interval_s: float = _POLL_INTERVAL_S,
) -> None:
    """Poll the Goose pane and mirror its approval prompts to web elicitations.

    :param base_url: Server base URL.
    :param headers: Auth/routing headers for the runner's requests.
    :param session_id: Omnigent conversation id.
    :param bridge_dir: The goose-native bridge dir holding ``tmux.json``.
    :param auth: Optional httpx auth for the runner's requests.
    :param poll_interval_s: Pane poll cadence in seconds.
    """
    active: dict[str, object] | None = None
    episode = 0
    timeout = httpx.Timeout(_POST_TIMEOUT_S, connect=10.0)
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        while True:
            try:
                pane = await asyncio.to_thread(capture_goose_pane, bridge_dir)
                prompt = parse_goose_approval_prompt(pane) if pane else None
                if prompt is not None:
                    # Rising edge only: ONE card per visible-prompt episode. We do
                    # NOT re-mint while the prompt stays up — the scraped tool
                    # context above the cliclack widget jitters across polls, and
                    # keying on it previously parked a fresh card every poll (all
                    # left dangling when the TUI answer cleared only the latest).
                    if active is None:
                        episode += 1
                        elicitation_id = goose_permission_elicitation_id(session_id, str(episode))
                        task = asyncio.create_task(
                            _run_one_approval(
                                client,
                                session_id=session_id,
                                bridge_dir=bridge_dir,
                                prompt=prompt,
                                elicitation_id=elicitation_id,
                            ),
                            name=f"goose-approval-{episode}",
                        )
                        active = {"elicitation_id": elicitation_id, "task": task}
                elif active is not None:
                    # Falling edge: the prompt vanished. If the web card is still
                    # parked (answered in the TUI), release it; if the task already
                    # finished (answered via the web verdict), nothing to do.
                    task = active["task"]
                    if isinstance(task, asyncio.Task) and not task.done():
                        await _post_external_elicitation_resolved(
                            client, session_id, str(active["elicitation_id"])
                        )
                    active = None
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "goose approval mirror poll failed; session=%s bridge_dir=%s",
                    session_id,
                    bridge_dir,
                )
            await asyncio.sleep(poll_interval_s)


async def _run_one_approval(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    prompt: GooseApprovalPrompt,
    elicitation_id: str,
) -> None:
    """Park one Goose prompt on the server and drive the cliclack selector."""
    payload = {
        "elicitation_id": elicitation_id,
        "agent": "Goose",
        "policy_name": "goose_native_permission",
        "operation_type": "tool",
        "message": prompt.message,
        "content_preview": prompt.preview,
    }
    try:
        response = await client.post(
            f"/v1/sessions/{session_id}/hooks/native-permission-request",
            json=payload,
        )
    except httpx.HTTPError:
        _logger.exception("goose permission hook POST failed; session=%s", session_id)
        return
    if response.status_code >= 400:
        _logger.warning(
            "goose permission hook rejected: status=%s body=%s",
            response.status_code,
            response.text[:512],
        )
        return
    if not response.content:
        return
    try:
        result = response.json()
    except ValueError:
        _logger.warning("goose permission hook returned non-JSON: %s", response.text[:512])
        return
    action = result.get("action") if isinstance(result, dict) else None
    # Drive the cliclack radio: Enter selects the default-highlighted "Allow";
    # Down×N + Enter selects "Deny". A decline/cancel verdict both map to Deny.
    keys: tuple[str, ...] | None = None
    if action == "accept":
        keys = ("Enter",)
    elif action in {"decline", "cancel"}:
        keys = (*(["Down"] * prompt.deny_down_count), "Enter")
    if keys is None:
        return
    try:
        await asyncio.to_thread(send_goose_pane_keys, bridge_dir, *keys)
    except RuntimeError:
        _logger.exception(
            "failed to send goose approval keystrokes %r; session=%s", keys, session_id
        )


async def _post_external_elicitation_resolved(
    client: httpx.AsyncClient, session_id: str, elicitation_id: str
) -> None:
    """Tell the server the native TUI answered a pending Goose prompt."""
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
                "goose external_elicitation_resolved rejected: status=%s body=%s",
                response.status_code,
                response.text[:512],
            )
    except httpx.HTTPError:
        _logger.exception("goose external_elicitation_resolved POST failed")
