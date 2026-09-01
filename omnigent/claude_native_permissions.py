"""Claude-native permission mirror (pane → web elicitation) — the safety net.

Claude Code's own ``PermissionRequest`` hook is the primary route for getting
a tool permission prompt into the Omnigent web UI: it fires just before the TUI
renders the prompt, parks server-side for a day, and the web card and the
terminal prompt then race (see :mod:`omnigent.claude_native_hook` and the
``/hooks/permission-request`` route). Both surfaces are live by design and
whichever is answered first wins.

When that route fails, though, it fails *silently*. The hook exits 0 on every
transport error so Claude keeps its own prompt, Claude Code discards a
zero-exit hook's stderr, and a card that was published but never reached a
subscriber leaves nothing behind either. The session then blocks on a prompt
that exists only in the embedded terminal, with the web UI showing no card and
no hint that anything is waiting — observed in the field as a turn stalled for
nearly three hours.

This watcher closes that hole:

1. poll ``capture-pane`` and parse Claude's permission prompt — the block
   titled by the gated tool ("Bash command") with a
   ``Do you want to …?`` question and NUMBERED options (``❯ 1. Yes`` … ``3.
   No``), where pressing the digit both selects and confirms,
2. once the same prompt has been on screen for
   :data:`_UNSURFACED_GRACE_S`, ask the server whether a prompt is already
   pending for the session — the hook needs a moment to park, and a card it
   parked must not be duplicated,
3. only when the server reports nothing pending, POST the prompt to the
   generic ``native-permission-request`` hook so a card renders, and send the
   matching digit into the pane on the web verdict,
4. if the prompt vanishes while our card is still parked (answered in the
   terminal), POST ``external_elicitation_resolved`` so the card clears.

The prompt in the pane always stays authoritative: this never dismisses it and
never answers it without a web verdict, so a detection miss degrades to
exactly the old behaviour. Mirrors :mod:`omnigent.hermes_native_permissions`.

Deliberately narrow: only the plain ``Yes`` / ``No`` shape is mirrored. Prompts
whose first option carries a rider ("Yes, and auto-accept edits", the
``ExitPlanMode`` review) are left to the hook, which renders them as purpose-
built cards with the full tool input rather than a scraped preview.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

import httpx

from omnigent.claude_native_bridge import (
    PERMISSION_PROMPT_HINT,
    capture_claude_pane,
    read_recent_permission_traces,
    send_claude_pane_keys,
)


class _PendingApproval(TypedDict):
    elicitation_id: str
    task: asyncio.Task[None]


_logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.5
# How long the same prompt must stay on screen before the mirror considers
# stepping in. It only has to cover the hook's park round-trip (one local
# POST), and a prompt still unsurfaced after this long is already a stall.
_UNSURFACED_GRACE_S = 5.0
# While a prompt stays up unsurfaced, how often to re-ask the server whether a
# card exists. Covers a hook that parked first and gave up later (its retry
# budget can run out mid-prompt), without polling the snapshot on every frame.
_RECHECK_INTERVAL_S = 30.0
# The hook parks server-side until a human answers; allow a day so this POST
# never abandons a live prompt.
_POST_TIMEOUT_S = 86400.0
# Budget for the "is a card already pending?" snapshot read.
_SNAPSHOT_TIMEOUT_S = 20.0

# One numbered option row, e.g. "❯ 1. Yes" or "  3. No". Claude renders the
# selection caret on the active row only.
_OPTION_RE = re.compile(r"^\s*(?:❯\s*)?(?P<num>\d)\.\s+(?P<label>\S.*?)\s*$")
# The rule Claude draws above the prompt block, which bounds the preview scan.
_RULE_RE = re.compile(r"^[\s─]*─{8,}[\s─]*$")
# Footer Claude renders under the options ("Esc to cancel · Tab to amend").
# Required, not just used to end the option scan: it is what separates a live
# prompt from Claude's own prose that happens to quote a question and an option
# list. A false positive would put a stray digit in the composer.
_FOOTER_HINT = "Esc to cancel"


@dataclass(frozen=True)
class ClaudePermissionPrompt:
    """A Claude Code tool permission prompt parsed from the pane.

    :param title: The prompt's own title, i.e. the gated tool as Claude
        labels it, e.g. ``"Bash command"``.
    :param question: The question row, e.g. ``"Do you want to proceed?"``.
    :param preview: Compact preview of the gated call for the card.
    :param accept_key: Digit that selects+confirms the plain "Yes" row.
    :param decline_key: Digit that selects+confirms the "No" row.
    :param signature: Stable hash of the prompt's visible content. Identifies
        one prompt episode across re-renders, and distinguishes a *new* prompt
        that replaced it without an intervening empty frame.
    """

    title: str
    question: str
    preview: str
    accept_key: str
    decline_key: str
    signature: str


def claude_permission_elicitation_id(session_id: str, signature: str) -> str:
    """
    Return the deterministic elicitation id for a mirrored pane prompt.

    Keyed by the prompt's content signature rather than a counter so a
    re-POST for the same on-screen prompt re-parks the same elicitation
    instead of minting a second card.

    :param session_id: Omnigent conversation id.
    :param signature: The parsed prompt's :attr:`ClaudePermissionPrompt.signature`.
    :returns: Elicitation id, e.g. ``"elicit_claude_pane_conv_x_ab12…"``.
    """
    return f"elicit_claude_pane_{session_id}_{signature}"


def _prompt_preview_lines(lines: list[str], question_idx: int) -> list[str]:
    """
    Collect the prompt block's content rows above its question.

    Scans up to the rule Claude draws above the block and drops its
    "Tip: auto mode handles these prompts for you" advice (which wraps
    onto a continuation line, hence skipping to the next blank row).

    :param lines: The captured pane split into lines.
    :param question_idx: Index of the ``Do you want to …`` row.
    :returns: Content rows, in screen order, e.g.
        ``["Bash command", "for d in a b; do echo $d; done"]``.
    """
    start = 0
    for index in range(question_idx - 1, -1, -1):
        if _RULE_RE.match(lines[index]):
            start = index + 1
            break
    collected: list[str] = []
    skipping_tip = False
    for line in lines[start:question_idx]:
        text = line.strip()
        if not text:
            skipping_tip = False
            continue
        if skipping_tip:
            continue
        if text.startswith("Tip:"):
            skipping_tip = True
            continue
        collected.append(text)
    return collected


def parse_claude_permission_prompt(pane: str) -> ClaudePermissionPrompt | None:
    """
    Parse Claude Code's tool permission prompt from pane text.

    Requires the whole live signature — a ``Do you want to …`` question, a
    plain ``Yes`` row and a ``No`` row numbered *below* it, and the prompt's
    own footer below those — so neither a transcript line quoting the question
    nor a half-repainted frame is mistaken for a prompt awaiting an answer.

    :param pane: Visible pane text from ``capture-pane -p``.
    :returns: The parsed prompt, or ``None`` when no answerable prompt is
        visible (including the option shapes this mirror leaves to the hook).
    """
    if not pane or PERMISSION_PROMPT_HINT not in pane:
        return None
    lines = pane.splitlines()
    question_idx = next(
        (
            index
            for index in range(len(lines) - 1, -1, -1)
            if PERMISSION_PROMPT_HINT in lines[index]
        ),
        None,
    )
    if question_idx is None:
        return None

    accept_key: str | None = None
    decline_key: str | None = None
    options: list[str] = []
    footer_seen = False
    for line in lines[question_idx + 1 :]:
        if _FOOTER_HINT in line:
            footer_seen = True
            break
        match = _OPTION_RE.match(line)
        if match is None:
            continue
        label = match.group("label")
        options.append(f"{match.group('num')}. {label}")
        folded = label.casefold()
        if accept_key is None and folded == "yes":
            accept_key = match.group("num")
        elif decline_key is None and folded.startswith("no"):
            decline_key = match.group("num")
    if not footer_seen or accept_key is None or decline_key is None:
        return None

    content = _prompt_preview_lines(lines, question_idx)
    title = content[0] if content else "tool call"
    question = lines[question_idx].strip()
    preview = " · ".join(content)[:1024]
    signature = hashlib.sha256(
        "\n".join([question, *content, *options]).encode("utf-8")
    ).hexdigest()[:16]
    return ClaudePermissionPrompt(
        title=title,
        question=question,
        preview=preview or title,
        accept_key=accept_key,
        decline_key=decline_key,
        signature=signature,
    )


async def _web_prompt_pending(client: httpx.AsyncClient, session_id: str) -> bool | None:
    """
    Report whether the session already has a prompt awaiting a web answer.

    :param client: Server client for the runner's requests.
    :param session_id: Omnigent conversation id.
    :returns: ``True`` when the snapshot lists a pending elicitation,
        ``False`` when it lists none, ``None`` when it could not be read
        (callers stand down and retry rather than risk a duplicate card).
    """
    try:
        response = await client.get(
            f"/v1/sessions/{session_id}",
            timeout=_SNAPSHOT_TIMEOUT_S,
        )
    except httpx.HTTPError:
        _logger.debug(
            "claude permission mirror: session snapshot unreadable; session=%s",
            session_id,
            exc_info=True,
        )
        return None
    if response.status_code >= 400:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    pending = body.get("pending_elicitations")
    return bool(pending) if isinstance(pending, list) else None


async def supervise_claude_permission_mirror(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    auth: httpx.Auth | None = None,
    poll_interval_s: float = _POLL_INTERVAL_S,
    grace_s: float = _UNSURFACED_GRACE_S,
    recheck_interval_s: float = _RECHECK_INTERVAL_S,
) -> None:
    """
    Watch the Claude pane and surface prompts the hook route never delivered.

    Runs for the session's lifetime (cancelled on teardown). At most one
    prompt is mirrored at a time. A prompt the hook already parked is left
    alone: this only steps in once the server confirms the session has no
    pending prompt, so the web UI cannot end up with two cards for one call.

    :param base_url: Server base URL.
    :param headers: Auth/routing headers for the runner's requests.
    :param session_id: Omnigent conversation id.
    :param bridge_dir: The claude-native bridge dir holding ``tmux.json``.
    :param auth: Optional httpx auth for the runner's requests.
    :param poll_interval_s: Pane poll cadence in seconds.
    :param grace_s: How long a prompt must persist before stepping in.
    :param recheck_interval_s: How often to re-ask the server about a prompt
        that is still up and still unsurfaced.
    :returns: None. Runs until cancelled.
    """
    from omnigent.cli_auth import open_server_client

    active: _PendingApproval | None = None
    # Signature of the prompt currently on screen, when it first appeared,
    # and when the server was last asked about it.
    visible_signature: str | None = None
    visible_since = 0.0
    last_checked_at = 0.0
    timeout = httpx.Timeout(_POST_TIMEOUT_S, connect=10.0)

    async with open_server_client(base_url, headers=headers, auth=auth, timeout=timeout) as client:
        while True:
            try:
                pane = await asyncio.to_thread(capture_claude_pane, bridge_dir)
                prompt = parse_claude_permission_prompt(pane) if pane else None
                now = time.monotonic()

                if prompt is None or prompt.signature != visible_signature:
                    # Falling edge (or replaced by a different prompt): release
                    # a card we parked, since the pane no longer awaits it.
                    if active is not None:
                        task = active["task"]
                        if isinstance(task, asyncio.Task) and not task.done():
                            await _post_external_elicitation_resolved(
                                client, session_id, str(active["elicitation_id"])
                            )
                        active = None
                    visible_signature = prompt.signature if prompt is not None else None
                    visible_since = now
                    last_checked_at = 0.0

                if (
                    prompt is not None
                    and active is None
                    and now - visible_since >= grace_s
                    and now - last_checked_at >= recheck_interval_s
                ):
                    last_checked_at = now
                    # Short-circuit: only re-read the pane when the server has
                    # confirmed there is no card, so the common "hook owns it"
                    # path costs one snapshot read and no extra tmux spawn.
                    if await _web_prompt_pending(client, session_id) is False and (
                        await asyncio.to_thread(_prompt_still_shown, bridge_dir, prompt.signature)
                    ):
                        _logger.warning(
                            "claude permission prompt has no web card after %.0fs — "
                            "surfacing it from the pane; session=%s title=%r "
                            "hook_trace=%s",
                            now - visible_since,
                            session_id,
                            prompt.title,
                            [
                                entry.get("outcome")
                                for entry in read_recent_permission_traces(bridge_dir, limit=3)
                            ],
                        )
                        elicitation_id = claude_permission_elicitation_id(
                            session_id, prompt.signature
                        )
                        task = asyncio.create_task(
                            _run_one_approval(
                                client,
                                session_id=session_id,
                                bridge_dir=bridge_dir,
                                prompt=prompt,
                                elicitation_id=elicitation_id,
                            ),
                            name=f"claude-permission-{prompt.signature}",
                        )
                        active = {"elicitation_id": elicitation_id, "task": task}
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "claude permission mirror poll failed; session=%s bridge_dir=%s",
                    session_id,
                    bridge_dir,
                )
            await asyncio.sleep(poll_interval_s)


def _prompt_still_shown(bridge_dir: Path, signature: str) -> bool:
    """
    Re-read the pane and confirm the same prompt is still awaiting an answer.

    Spent at both points where acting on a stale read would misfire: before
    minting a card (a prompt answered during the snapshot round-trip must not
    raise one after the fact) and before sending a verdict digit (which at a
    bare composer would be typed into the person's next message).

    :param bridge_dir: The claude-native bridge dir holding ``tmux.json``.
    :param signature: Signature of the prompt being acted on.
    :returns: ``True`` when the same prompt is still on screen.
    """
    pane = capture_claude_pane(bridge_dir)
    prompt = parse_claude_permission_prompt(pane) if pane else None
    return prompt is not None and prompt.signature == signature


async def _run_one_approval(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    prompt: ClaudePermissionPrompt,
    elicitation_id: str,
) -> None:
    """
    Park one pane-detected prompt on the server and send the verdict digit.

    :param client: Server client for the runner's requests.
    :param session_id: Omnigent conversation id.
    :param bridge_dir: The claude-native bridge dir holding ``tmux.json``.
    :param prompt: The parsed prompt being mirrored.
    :param elicitation_id: Stable id the card is parked under.
    :returns: None.
    """
    payload = {
        "elicitation_id": elicitation_id,
        "agent": "Claude Code",
        "policy_name": "claude_native_permission",
        "operation_type": prompt.title,
        "message": f"Claude is waiting in the terminal: {prompt.question}",
        "content_preview": prompt.preview,
        # A decline is answered with the prompt's own "No" digit, which hands
        # Claude a denial and lets the turn continue. Interrupting as well
        # would abort the turn the person only meant to redirect.
        "interrupt_on_decline": False,
    }
    try:
        response = await client.post(
            f"/v1/sessions/{session_id}/hooks/native-permission-request",
            json=payload,
        )
    except httpx.HTTPError:
        _logger.exception("claude permission mirror POST failed; session=%s", session_id)
        return
    if response.status_code >= 400:
        _logger.warning(
            "claude permission mirror rejected: status=%s body=%s",
            response.status_code,
            response.text[:512],
        )
        return
    if not response.content:
        # Empty 2xx → answered in the terminal, or timed out: no keystroke.
        return
    try:
        result = response.json()
    except ValueError:
        _logger.warning("claude permission mirror got non-JSON: %s", response.text[:512])
        return
    action = result.get("action") if isinstance(result, dict) else None
    if action == "accept":
        key = prompt.accept_key
    elif action in {"decline", "cancel"}:
        key = prompt.decline_key
    else:
        return
    # Only answer a prompt that is verifiably still the one on screen — a
    # digit sent at a bare composer would be typed into the person's message.
    if not await asyncio.to_thread(_prompt_still_shown, bridge_dir, prompt.signature):
        _logger.info(
            "claude permission prompt vanished before the web verdict landed; "
            "session=%s action=%s",
            session_id,
            action,
        )
        return
    try:
        await asyncio.to_thread(send_claude_pane_keys, bridge_dir, key)
    except RuntimeError:
        _logger.exception(
            "failed to send claude approval keystroke %r; session=%s", key, session_id
        )


async def _post_external_elicitation_resolved(
    client: httpx.AsyncClient, session_id: str, elicitation_id: str
) -> None:
    """
    Tell the server the terminal answered a prompt this mirror had parked.

    :param client: Server client for the runner's requests.
    :param session_id: Omnigent conversation id.
    :param elicitation_id: Id of the card to release.
    :returns: None.
    """
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
                "claude external_elicitation_resolved rejected: status=%s body=%s",
                response.status_code,
                response.text[:512],
            )
    except httpx.HTTPError:
        _logger.exception("claude external_elicitation_resolved POST failed")
