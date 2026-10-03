"""Drive a native session into a known phase with scripted model turns.

Each turn carries a unique marker. The mock model serves its replies only to
the main-loop request whose user message contains that marker, and the shell
commands it scripts leave marker-named files in the workspace, so a scenario
can tell exactly which side effects ran and how often.

Harness differences stay here:

- Claude Code runs one ``Bash`` call for the whole tool phase; its permission
  hook asks for approval, which the driver accepts as a user would.
- Codex's ``exec_command`` yields after at most 30 s, so a long tool phase is a
  chain of shorter calls. Workspace commands run without approval in the
  "Ask for approval" mode the lab launches Codex in. An approval prompt comes
  from an escalated (``require_escalated``) command.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from tests.e2e.resilience.lab.lab import MAIN_LOOP_TOOL, Harness, Lab, message_text, wait_for

_TURN_TIMEOUT_S = 120.0
# Claude Code's maximum Bash timeout.
_BASH_TIMEOUT_MS = 600_000
# Codex's exec_command waits at most 30 s before yielding; keep steps under it.
_CODEX_STEP_S = 25.0
_CODEX_YIELD_MS = 30_000
#: Codex launch args for "Ask for approval": escalations reach the user, not
#: Codex's automatic reviewer.
CODEX_ASK_FOR_APPROVAL = ["--ask-for-approval", "on-request"]


@dataclass(frozen=True)
class Turn:
    """One scripted user turn.

    :param marker: Unique token in the user message and every scripted reply.
    :param reply: Final assistant text the turn ends with.
    :param started: Workspace file the turn's tool creates when it starts, if any.
    :param done: Workspace file the turn's tool creates when it finishes, if any.
    :param call_ids: Scripted tool-call ids, in order.
    :param tool_s: How long the turn's tools run in total, e.g. ``140``.
    """

    marker: str
    reply: str
    started: Path | None = None
    done: Path | None = None
    call_ids: tuple[str, ...] = field(default_factory=tuple)
    tool_s: float = 0.0


class SessionDriver:
    """Script and send native-session turns as the user would.

    :param lab: Running lab.
    :param session_id: Session to drive.
    :param harness: The session's harness.
    """

    def __init__(self, lab: Lab, session_id: str, harness: Harness = "claude") -> None:
        self.lab = lab
        self.session_id = session_id
        self.harness: Harness = harness

    @classmethod
    def create(cls, lab: Lab, harness: Harness) -> SessionDriver:
        """Create a session for *harness* in the lab's scenario mode and drive it."""
        launch_args = CODEX_ASK_FOR_APPROVAL if harness == "codex" else None
        return cls(lab, lab.create_session(harness, launch_args=launch_args), harness)

    # ── phases ───────────────────────────────────────────────────

    def round_trip(self, timeout: float = _TURN_TIMEOUT_S) -> Turn:
        """Run a text-only turn to completion."""
        turn = self.text_turn()
        self.send(turn)
        self.wait_done(turn, timeout=timeout)
        return turn

    def text_turn(self) -> Turn:
        """Script a text-only turn without sending it."""
        turn = self._turn()
        self._script(turn, [{"text": turn.reply}])
        return turn

    def start_tool_turn(self, seconds: float) -> Turn:
        """Start a turn whose shell tool runs for *seconds*; return once it is running.

        Any approval prompt for the tool is accepted, as a user would.
        """
        turn = self._tool_turn(seconds)
        self.send(turn)
        self.approve_until(turn, lambda: turn.started is not None and turn.started.exists())
        return turn

    def start_approval_turn(self) -> tuple[Turn, str]:
        """Start a turn that stops at an approval prompt.

        :returns: The waiting turn and the pending approval's id.
        """
        marker = self._marker()
        done = self.lab.workspace / f"done-{marker}"
        call = self._shell_call(marker, 0, f"touch {done.name}", escalate=True)
        turn = Turn(marker, _reply(marker), None, done, (call["call_id"],))
        self._script(turn, [{"tool_calls": [call]}, {"text": turn.reply}])
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
        """
        turn = self._turn()
        words = 40
        text = " ".join(f"word{i}" for i in range(words)) + f" {turn.reply}"
        reply = {"text": text, "stream": True, "chunk_delay": seconds / (words + 5)}
        self._script(turn, [reply] * (1 + retries))
        self.send(turn)
        wait_for(
            lambda: True if self.model_calls(turn) else None,
            timeout=_TURN_TIMEOUT_S,
            what=f"the model call for {turn.marker}",
        )
        return turn

    # ── user actions ─────────────────────────────────────────────

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

    def pending_approval(self, turn: Turn) -> dict[str, Any] | None:
        """The pending approval prompt for *turn*, read directly from the server."""
        snapshot = self.lab.snapshot(self.session_id)
        for pending in snapshot.get("pending_elicitations") or []:
            if turn.marker in json.dumps(pending.get("params", {})):
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

    # ── observations ─────────────────────────────────────────────

    def wait_done(self, turn: Turn, *, timeout: float = _TURN_TIMEOUT_S) -> None:
        """Wait until the turn's final reply is committed."""
        self.lab.wait_for_text(self.session_id, turn.reply, timeout=timeout)

    def count_text(self, needle: str, *, role: str | None = None) -> int:
        """Count committed messages containing *needle*, optionally only from *role*."""
        return sum(
            1
            for item in self.lab.items(self.session_id)
            if item.get("type") == "message"
            and (role is None or item.get("role") == role)
            and needle in message_text(item)
        )

    def tool_output_counts(self, turn: Turn) -> dict[str, int]:
        """Committed tool results per scripted call id."""
        counts = dict.fromkeys(turn.call_ids, 0)
        for item in self.lab.items(self.session_id):
            if item.get("type") == "function_call_output" and item.get("call_id") in counts:
                counts[item["call_id"]] += 1
        return counts

    def model_calls(self, turn: Turn) -> int:
        """How many main-loop model requests have carried *turn*'s message so far."""
        assert self.lab.model is not None
        tool = MAIN_LOOP_TOOL[self.harness]
        count = 0
        for request in self.lab.model.requests():
            tools = request.get("tools") if isinstance(request.get("tools"), list) else []
            if any(isinstance(t, dict) and t.get("name") == tool for t in tools) and (
                turn.marker in json.dumps(request.get("messages") or request.get("input") or "")
            ):
                count += 1
        return count

    # ── scripting ────────────────────────────────────────────────

    def _tool_turn(self, seconds: float) -> Turn:
        marker = self._marker()
        started = self.lab.workspace / f"started-{marker}"
        done = self.lab.workspace / f"done-{marker}"
        if self.harness == "claude":
            commands = [f"touch {started.name} && sleep {seconds:g} && touch {done.name}"]
        else:
            steps = max(1, math.ceil(seconds / _CODEX_STEP_S))
            step_s = seconds / steps
            commands = [f"sleep {step_s:g}" for _ in range(steps)]
            commands[0] = f"touch {started.name} && {commands[0]}"
            commands[-1] = f"{commands[-1]} && touch {done.name}"
        calls = [self._shell_call(marker, index, cmd) for index, cmd in enumerate(commands)]
        turn = Turn(
            marker, _reply(marker), started, done, tuple(c["call_id"] for c in calls), seconds
        )
        self._script(turn, [*({"tool_calls": [call]} for call in calls), {"text": turn.reply}])
        return turn

    def _shell_call(
        self, marker: str, index: int, command: str, *, escalate: bool = False
    ) -> dict[str, str]:
        call_id = f"call_{marker.lower()}_{index}"
        if self.harness == "claude":
            # Claude Code kills Bash commands after two minutes unless told otherwise.
            arguments: dict[str, object] = {
                "command": command,
                "description": f"task {marker}",
                "timeout": _BASH_TIMEOUT_MS,
            }
            return {"call_id": call_id, "name": "Bash", "arguments": json.dumps(arguments)}
        arguments = {"cmd": command, "yield_time_ms": _CODEX_YIELD_MS}
        if escalate:
            arguments.update(
                sandbox_permissions="require_escalated", justification=f"task {marker}"
            )
        return {"call_id": call_id, "name": "exec_command", "arguments": json.dumps(arguments)}

    def _script(self, turn: Turn, responses: list[dict[str, Any]]) -> None:
        self.lab.script_turn(turn.marker, responses, harness=self.harness)

    def _turn(self) -> Turn:
        marker = self._marker()
        return Turn(marker, _reply(marker))

    @staticmethod
    def _marker() -> str:
        return f"RL{uuid.uuid4().hex[:10].upper()}"


def _reply(marker: str) -> str:
    return f"Finished {marker}."
