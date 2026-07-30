"""Hermes-native tool-approval mirror (TUI → web elicitation).

The native ``hermes`` TUI gates commands it flags as dangerous with an
in-terminal approval prompt (its own ``tools/approval.py`` gate). That prompt
lives only in the TUI; to also surface it in the Omnigent web UI (so a user can
approve from the chat view, not only the embedded terminal), the runner watches
the Hermes pane:

1. poll ``capture-pane`` and detect the approval PANEL — the interactive TUI
   renders a prompt_toolkit panel titled ``⚠️  Dangerous Command`` with NUMBERED
   choices (``❯ 1. Allow once`` … ``4. Deny``), NOT the legacy ``Choice
   [o/s/a/D]:`` ``input()`` prompt (that path is fail-closed while prompt_toolkit
   owns the terminal). Verified against hermes-agent ``cli.py``
   ``_get_approval_display_fragments`` + the number-key bindings,
2. POST it to the server's generic ``native-permission-request`` hook, which
   publishes ``response.elicitation_request`` and parks for the web verdict,
3. on the verdict, send the choice's DIGIT key (e.g. ``1`` = Allow once, ``4`` =
   Deny) into the pane — Hermes' number-key binding selects AND confirms in one
   press,
4. if the panel instead disappears on its own (answered in the embedded
   terminal), POST ``external_elicitation_resolved`` so the parked web card
   clears.

This does NOT suppress Hermes' own gate — its panel stays the source of truth
and the fallback if pane detection ever fails (the user can still pick in the
terminal). Mirrors :mod:`omnigent.cursor_native_permissions`. NB: Hermes only
prompts for commands it flags *dangerous* (and may auto-approve low-risk ones via
smart-approval), so non-dangerous tools won't raise a card.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from omnigent.hermes_native_bridge import capture_hermes_pane, send_hermes_pane_keys

_logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.3
# The hook parks server-side until a human answers; allow a day so the runner's
# POST never abandons a live prompt.
_POST_TIMEOUT_S = 86400.0

# Hermes' interactive TUI renders the dangerous-command gate as a prompt_toolkit
# PANEL (cli.py ``_get_approval_display_fragments``), NOT the legacy ``input()``
# ``Choice [o/s/a/D]:`` prompt — that path is fail-closed while prompt_toolkit
# owns the terminal. The panel is titled ``⚠️  Dangerous Command`` and lists
# NUMBERED choices (``❯ 1. Allow once`` … ``4. Deny``); pressing the number both
# selects and confirms (cli.py number-key bindings call _handle_approval_selection).
# So we detect the panel by title + numbered choices and answer with the digit.
_TITLE_RE = re.compile(r"Dangerous Command", re.IGNORECASE)
# A numbered choice row, e.g. "❯ 1. Allow once" / "  4. Deny" (box borders ignored).
_CHOICE_RE = re.compile(
    r"(?P<num>\d)\.\s*(?P<label>Allow once|Allow for this session|"
    r"Add to permanent allowlist|Deny)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HermesApprovalPrompt:
    """A parsed Hermes dangerous-command approval panel.

    :param command: Best-effort command/description preview from the panel.
    :param message: Human-readable card message.
    :param preview: Compact preview for the card.
    :param accept_key: digit key that selects+confirms "Allow once" (e.g. ``"1"``).
    :param decline_key: digit key that selects+confirms "Deny" (e.g. ``"4"`` with
        the permanent-allowlist option, else ``"3"``).
    :param block_hash: Stable hash of the preview (kept for debugging/preview).
    """

    command: str
    message: str
    preview: str
    accept_key: str
    decline_key: str
    block_hash: str


def hermes_permission_elicitation_id(session_id: str, token: str) -> str:
    """Return the deterministic Omnigent elicitation id for a Hermes prompt.

    *token* identifies one approval episode (a per-session counter), not the
    scraped content, so a re-render never spawns a duplicate card.
    """
    return f"elicit_hermes_{session_id}_{token}"


def _strip_border(line: str) -> str:
    """Strip the panel's box-drawing borders/padding from *line*."""
    return line.strip().strip("│").strip()


def parse_hermes_approval_prompt(pane: str) -> HermesApprovalPrompt | None:
    """Parse a Hermes ``⚠️  Dangerous Command`` approval panel from pane text.

    Requires the panel title AND both an "Allow once" and a "Deny" numbered
    choice (so a title lingering without the live choice list is not re-detected),
    and reads the digit key for each from the panel itself — robust to whether the
    permanent-allowlist option is offered (Deny is ``4`` with it, ``3`` without).

    :param pane: Visible pane text from ``capture-pane -p``.
    :returns: The parsed prompt, or ``None`` when no live panel is visible.
    """
    if not pane or not _TITLE_RE.search(pane):
        return None
    lines = pane.splitlines()
    label_to_key: dict[str, str] = {}
    first_choice_idx: int | None = None
    for i, line in enumerate(lines):
        match = _CHOICE_RE.search(line)
        if match:
            label_to_key.setdefault(match.group("label").lower(), match.group("num"))
            if first_choice_idx is None:
                first_choice_idx = i
    accept_key = label_to_key.get("allow once")
    decline_key = label_to_key.get("deny")
    if accept_key is None or decline_key is None or first_choice_idx is None:
        return None

    # Best-effort command/description preview: the panel lines between the title
    # and the first choice, minus borders/blank/title lines.
    title_idx = next((i for i, line in enumerate(lines) if _TITLE_RE.search(line)), 0)
    preview_parts: list[str] = []
    for line in lines[title_idx + 1 : first_choice_idx]:
        text = _strip_border(line)
        if text and not _TITLE_RE.search(text):
            preview_parts.append(text)
    preview = " ".join(preview_parts)[:1024]
    block_hash = hashlib.sha256(preview.encode("utf-8")).hexdigest()[:16]
    return HermesApprovalPrompt(
        command=preview,
        message="Hermes flagged a dangerous command. Run it?",
        preview=preview or "dangerous command",
        accept_key=accept_key,
        decline_key=decline_key,
        block_hash=block_hash,
    )


async def supervise_hermes_approval_mirror(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    auth: httpx.Auth | None = None,
    poll_interval_s: float = _POLL_INTERVAL_S,
) -> None:
    """Poll the Hermes pane and mirror its approval prompts to web elicitations.

    Runs for the session's lifetime (cancelled on teardown). At most one prompt
    is active at a time: a new block spawns a task that parks on the server and,
    on the web verdict, sends the keystroke; a block that vanishes while still
    parked means the user answered in the TUI, so the parked card is released.

    :param base_url: Server base URL.
    :param headers: Auth/routing headers for the runner's requests.
    :param session_id: Omnigent conversation id.
    :param bridge_dir: The hermes-native bridge dir holding ``tmux.json``.
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
                pane = await asyncio.to_thread(capture_hermes_pane, bridge_dir)
                prompt = parse_hermes_approval_prompt(pane) if pane else None
                if prompt is not None:
                    # Rising edge only: ONE card per visible-prompt episode (do not
                    # re-mint while the prompt stays up).
                    if active is None:
                        episode += 1
                        elicitation_id = hermes_permission_elicitation_id(session_id, str(episode))
                        task = asyncio.create_task(
                            _run_one_approval(
                                client,
                                session_id=session_id,
                                bridge_dir=bridge_dir,
                                prompt=prompt,
                                elicitation_id=elicitation_id,
                            ),
                            name=f"hermes-approval-{episode}",
                        )
                        active = {"elicitation_id": elicitation_id, "task": task}
                elif active is not None:
                    # Falling edge: prompt vanished. Release the card if still
                    # parked (answered in the TUI); no-op if answered via the web.
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
                    "hermes approval mirror poll failed; session=%s bridge_dir=%s",
                    session_id,
                    bridge_dir,
                )
            await asyncio.sleep(poll_interval_s)


async def _run_one_approval(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    prompt: HermesApprovalPrompt,
    elicitation_id: str,
) -> None:
    """Park one Hermes prompt on the server and send the verdict keystroke."""
    payload = {
        "elicitation_id": elicitation_id,
        "agent": "Hermes",
        "policy_name": "hermes_native_permission",
        "operation_type": "shell",
        "message": prompt.message,
        "content_preview": prompt.preview,
    }
    try:
        response = await client.post(
            f"/v1/sessions/{session_id}/hooks/native-permission-request",
            json=payload,
        )
    except httpx.HTTPError:
        _logger.exception("hermes permission hook POST failed; session=%s", session_id)
        return
    if response.status_code >= 400:
        _logger.warning(
            "hermes permission hook rejected: status=%s body=%s",
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
        _logger.warning("hermes permission hook returned non-JSON: %s", response.text[:512])
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
        await asyncio.to_thread(send_hermes_pane_keys, bridge_dir, key)
    except RuntimeError:
        _logger.exception(
            "failed to send hermes approval keystroke %r; session=%s", key, session_id
        )


async def _post_external_elicitation_resolved(
    client: httpx.AsyncClient, session_id: str, elicitation_id: str
) -> None:
    """Tell the server the native TUI answered a pending Hermes prompt."""
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
                "hermes external_elicitation_resolved rejected: status=%s body=%s",
                response.status_code,
                response.text[:512],
            )
    except httpx.HTTPError:
        _logger.exception("hermes external_elicitation_resolved POST failed")
