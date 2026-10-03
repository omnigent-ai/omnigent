"""Drive a claude-native session into a known phase with scripted model turns.

Each turn carries a unique marker. The mock model serves its replies only to
the request whose user message contains that marker, and the Bash commands it
scripts leave marker-named files in the workspace, so a scenario can tell
exactly which side effects ran and how often.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from tests.e2e.resilience.lab.lab import Lab, wait_for

_TURN_TIMEOUT_S = 120.0
# Claude Code's maximum Bash timeout.
_BASH_TIMEOUT_MS = 600_000


@dataclass(frozen=True)
class Turn:
    """One scripted user turn.

    :param marker: Unique token in the user message and every scripted reply.
    :param reply: Final assistant text the turn ends with.
    :param started: Workspace file the turn's tool creates when it starts, if any.
    :param done: Workspace file the turn's tool creates when it finishes, if any.
    """

    marker: str
    reply: str
    started: Path | None = None
    done: Path | None = None


class ClaudeDriver:
    """Script and send claude-native turns as the user would.

    :param lab: Running lab.
    :param session_id: Claude session to drive.
    """

    def __init__(self, lab: Lab, session_id: str) -> None:
        self.lab = lab
        self.session_id = session_id

    def round_trip(self, timeout: float = _TURN_TIMEOUT_S) -> Turn:
        """Run a text-only turn to completion.

        :param timeout: Seconds to wait for the reply.
        :returns: The completed turn.
        """
        turn = self._turn()
        self.lab.script_turn(turn.marker, [{"text": turn.reply}])
        self.send(turn)
        self.wait_done(turn, timeout=timeout)
        return turn

    def start_tool_turn(self, seconds: float) -> Turn:
        """Start a turn whose Bash tool runs for *seconds*; return once it is running.

        Any approval prompt for the tool is accepted, as a user would.

        :param seconds: How long the tool runs, e.g. ``30``.
        :returns: The running turn.
        """
        turn = self._turn(with_files=True)
        assert turn.started is not None and turn.done is not None
        command = f"touch {turn.started.name} && sleep {seconds:g} && touch {turn.done.name}"
        self._script_bash(turn, command)
        self.send(turn)
        self.approve_until(turn, lambda: turn.started is not None and turn.started.exists())
        return turn

    def start_approval_turn(self) -> tuple[Turn, str]:
        """Start a turn that stops at a Bash approval prompt.

        :returns: The waiting turn and the pending approval's id.
        """
        turn = self._turn(with_files=True)
        assert turn.done is not None
        self._script_bash(turn, f"touch {turn.done.name}")
        self.send(turn)
        approval = wait_for(
            lambda: self.pending_approval(turn),
            timeout=_TURN_TIMEOUT_S,
            what=f"the approval prompt for {turn.marker}",
        )
        return turn, str(approval["elicitation_id"])

    def start_streaming_turn(self, seconds: float, *, retries: int = 3) -> Turn:
        """Start a text turn whose reply streams for about *seconds*; return once it streams.

        The reply is queued *retries* extra times so a harness that retries a
        dropped model stream still gets the same answer.

        :param seconds: Streaming duration, e.g. ``8``.
        :param retries: Extra copies of the reply for retried model calls.
        :returns: The streaming turn.
        """
        turn = self._turn()
        words = 40
        text = " ".join(f"word{i}" for i in range(words)) + f" {turn.reply}"
        reply = {"text": text, "chunk_delay": seconds / (words + 5)}
        self.lab.script_turn(turn.marker, [reply] * (1 + retries))
        self.send(turn)
        wait_for(
            lambda: True if self.model_calls(turn) else None,
            timeout=_TURN_TIMEOUT_S,
            what=f"the model call for {turn.marker}",
        )
        return turn

    def send(self, turn: Turn) -> httpx.Response:
        """Send the turn's user message through the client link."""
        response = self.send_raw(turn)
        assert response.status_code < 400, response.text
        return response

    def send_raw(self, turn: Turn, *, timeout: float = 90.0) -> httpx.Response:
        """Send the turn's user message and return the response, whatever it is."""
        return self.lab.send_message(
            self.session_id, f"Run the task for {turn.marker}", timeout=timeout
        )

    def text_turn(self) -> Turn:
        """Script a text-only turn without sending it."""
        turn = self._turn()
        self.lab.script_turn(turn.marker, [{"text": turn.reply}])
        return turn

    def model_calls(self, turn: Turn) -> int:
        """How many main-loop model requests have carried *turn*'s message so far."""
        assert self.lab.model is not None
        count = 0
        for request in self.lab.model.requests():
            tools = request.get("tools") if isinstance(request.get("tools"), list) else []
            if any(isinstance(t, dict) and t.get("name") == "Bash" for t in tools) and (
                turn.marker in json.dumps(request.get("messages", ""))
            ):
                count += 1
        return count

    def pending_approval(self, turn: Turn) -> dict[str, Any] | None:
        """The pending approval prompt for *turn*, read directly from the server."""
        snapshot = self.lab.snapshot(self.session_id)
        for pending in snapshot.get("pending_elicitations") or []:
            params = pending.get("params") if isinstance(pending.get("params"), dict) else {}
            if turn.marker in str(params.get("content_preview", "")):
                return dict(pending)
        return None

    def approve(self, elicitation_id: str) -> httpx.Response:
        """Accept an approval prompt through the client link, as the web UI does."""
        assert self.lab.client is not None
        return self.lab.client.post(
            f"/v1/sessions/{self.session_id}/elicitations/{elicitation_id}/resolve",
            json={"action": "accept"},
        )

    def approve_until(self, turn: Turn, condition: Any, timeout: float = _TURN_TIMEOUT_S) -> None:
        """Accept *turn*'s approval prompts until *condition* holds."""
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                raise TimeoutError(f"{turn.marker} did not reach its phase within {timeout}s")
            pending = self.pending_approval(turn)
            if pending is not None:
                self.approve(str(pending["elicitation_id"])).raise_for_status()
            time.sleep(0.25)

    def wait_done(self, turn: Turn, *, timeout: float = _TURN_TIMEOUT_S) -> None:
        """Wait until the turn's final reply is committed."""
        self.lab.wait_for_text(self.session_id, turn.reply, timeout=timeout)

    def count_text(self, needle: str, *, role: str | None = None) -> int:
        """Count committed messages containing *needle*, optionally only from *role*."""
        from tests.e2e.resilience.lab.lab import message_text

        return sum(
            1
            for item in self.lab.items(self.session_id)
            if item.get("type") == "message"
            and (role is None or item.get("role") == role)
            and needle in message_text(item)
        )

    def count_tool_outputs(self, turn: Turn) -> int:
        """Count committed tool results for *turn*'s scripted call."""
        call_id = _call_id(turn)
        return sum(
            1
            for item in self.lab.items(self.session_id)
            if item.get("type") == "function_call_output" and item.get("call_id") == call_id
        )

    def _turn(self, *, with_files: bool = False) -> Turn:
        marker = f"RL{uuid.uuid4().hex[:10].upper()}"
        reply = f"Finished {marker}."
        if not with_files:
            return Turn(marker, reply)
        workspace = self.lab.workspace
        return Turn(
            marker,
            reply,
            started=workspace / f"started-{marker}",
            done=workspace / f"done-{marker}",
        )

    def _script_bash(self, turn: Turn, command: str) -> None:
        call = {
            "call_id": _call_id(turn),
            "name": "Bash",
            # Claude Code kills Bash commands after two minutes unless told otherwise.
            "arguments": json.dumps(
                {
                    "command": command,
                    "description": f"task {turn.marker}",
                    "timeout": _BASH_TIMEOUT_MS,
                }
            ),
        }
        self.lab.script_turn(turn.marker, [{"tool_calls": [call]}, {"text": turn.reply}])


def _call_id(turn: Turn) -> str:
    return f"toolu_{turn.marker.lower()}"
