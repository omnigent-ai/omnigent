"""Bounded session-only model selection through Claude Code's live picker."""

from __future__ import annotations

import contextlib
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


class ClaudeModelSwitchRefused(RuntimeError):
    """A client-safe refusal; the prompt is unsent but the live model may have changed."""

    def __init__(
        self,
        message: str,
        *,
        phase: str = "preflight",
        command_submitted: bool = False,
        model_may_have_changed: bool = False,
        unsupported: bool = False,
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.command_submitted = command_submitted
        self.model_may_have_changed = model_may_have_changed
        self.unsupported = unsupported


@dataclass(frozen=True)
class ClaudeModelSwitchResult:
    """A fresh local status observation for the exact requested selection."""

    requested_model: str
    picker_value: str
    resolved_model: str
    status_model: str
    session_id: str
    already_selected: bool = False


class ModelSwitchDeadline(threading.Event):
    """Apply one deadline to lock acquisition, pane reads and every key."""

    def __init__(
        self, seconds: float, parent: threading.Event | None, clock: Callable[[], float]
    ) -> None:
        super().__init__()
        self.clock = clock
        self.at = clock() + max(0.0, seconds)
        self.parent = parent

    def remaining_s(self) -> float:
        remaining = max(0.0, self.at - self.clock())
        parent_remaining = getattr(self.parent, "remaining_s", None)
        if callable(parent_remaining):
            parent_seconds = parent_remaining()
            if isinstance(parent_seconds, int | float):
                remaining = min(remaining, max(0.0, parent_seconds))
        return remaining

    def is_set(self) -> bool:
        return (
            super().is_set()
            or self.remaining_s() <= 0
            or (self.parent is not None and self.parent.is_set())
        )


@dataclass(frozen=True)
class _Target:
    value: str
    model: str
    label: str | None
    structured: bool


@dataclass(frozen=True)
class _PickerRow:
    number: int
    text: str
    selected: bool


@dataclass(frozen=True)
class _Picker:
    rows: tuple[_PickerRow, ...]
    selected: _PickerRow


_PICKER_TITLE = re.compile(r"(?m)^\s*Select model\s*$")
_PICKER_FOOTER = re.compile(
    r"(?m)^[ \t]*Enter\s+to\s+set\s+as\s+default\s*·\s*s\s+to\s+use\s+this\s+session\s+only"
    r"\s*·\s*Esc\s+to\s+cancel[ \t]*$"
)
_PICKER_ROW = re.compile(r"^\s*(❯)?\s*[↑↓]?\s*(\d+)\.\s+(.+)$")
_STATUS_TABS = re.compile(r"(?m)^\s*Settings\s+Status\s+Config\s+Usage\s+Stats\s*$")
_STATUS_ESCAPE = re.compile(r"(?m)^\s*(?:Press )?Esc to cancel\s*$")
_MIN_PICKER_VERSION = (2, 1, 323)
_IMMEDIATE_PICKER_TIMEOUT_S = 2.0


def _picker(pane: str) -> _Picker | None:
    titles = list(_PICKER_TITLE.finditer(pane))
    if not titles:
        return None
    region = pane[titles[-1].end() :]
    footer = _PICKER_FOOTER.search(region)
    if footer is None or region[footer.end() :].strip():
        return None
    rows: list[_PickerRow] = []
    for line in region[: footer.start()].splitlines():
        match = _PICKER_ROW.match(line)
        if match:
            rows.append(_PickerRow(int(match[2]), match[3].strip(), bool(match[1])))
        elif line.lstrip().startswith("❯"):
            return None
        elif rows and line.startswith("      ") and line.strip() and "…" not in line:
            previous = rows[-1]
            rows[-1] = _PickerRow(
                previous.number, previous.text + " " + line.strip(), previous.selected
            )
    selected = [row for row in rows if row.selected]
    numbers = [row.number for row in rows]
    if (
        len(selected) != 1
        or numbers != sorted(set(numbers))
        or any(number < 1 for number in numbers)
    ):
        return None
    return _Picker(tuple(rows), selected[0])


def _status(pane: str) -> dict[str, str] | None:
    tabs = list(_STATUS_TABS.finditer(pane))
    if not tabs:
        return None
    region = pane[tabs[-1].end() :]
    footer = _STATUS_ESCAPE.search(region)
    if footer is None or region[footer.end() :].strip():
        return None
    fields: dict[str, str] = {}
    for line in region[: footer.start()].splitlines():
        match = re.match(r"^\s*(Version|Session ID|Model):\s*(\S.*?)\s*$", line)
        if match:
            if match[1] in fields:
                return None
            fields[match[1]] = match[2]
    return fields if fields.keys() >= {"Version", "Session ID", "Model"} else None


def _draft(pane: str) -> str | None:
    from omnigent.harnesses.claude_native import bridge

    box = bridge._composer_box(pane)
    if box is None or not box.framed or bridge._occupying_surface(pane) is not None:
        return None
    first = box.rows[0].lstrip()
    if not first.startswith("❯"):
        return None
    indent = len(box.rows[0]) - len(first)
    continuation_prefix = " " * (indent + 2)
    if any(row.strip() and not row.startswith(continuation_prefix) for row in box.rows[1:]):
        return None
    return "\n".join(
        [first[1:].removeprefix(" "), *(row[len(continuation_prefix) :] for row in box.rows[1:])]
    )


def _known_claude_id(model: str) -> str | None:
    from omnigent.models.claude_model_vocabulary import canonical_claude_id

    if model.startswith(("claude-", "system.ai.claude-", "databricks-claude-")):
        return canonical_claude_id(model)
    return None


def _target(
    model: str,
    options: Sequence[Mapping[str, object]] | None,
    values: Sequence[str],
    env: Mapping[str, str],
) -> _Target:
    from omnigent.models.claude_model_vocabulary import alias_pins

    requested = model.strip()
    pins = alias_pins(env)
    base = requested.removesuffix("[1m]")
    pinned = pins.get(base)
    resolved = pinned or requested
    if requested.endswith("[1m]") and not resolved.endswith("[1m]"):
        resolved += "[1m]"
    candidates = [
        option
        for option in options or ()
        if option.get("enabled") is True and option.get("value") != "default"
    ]
    exact = [option for option in candidates if option.get("value") == requested]
    matched = exact or [option for option in candidates if option.get("model") == resolved]
    canonical = _known_claude_id(resolved)
    if not matched and canonical:
        matched = [
            option
            for option in candidates
            if isinstance(option.get("model"), str)
            and _known_claude_id(str(option["model"])) == canonical
        ]
        if not matched and not resolved.endswith("[1m]"):
            matched = [
                option
                for option in candidates
                if isinstance(option.get("model"), str)
                and _known_claude_id(str(option["model"]).removesuffix("[1m]")) == canonical
            ]
    if len(matched) == 1:
        option = matched[0]
        value, concrete = option.get("value"), option.get("model")
        if isinstance(value, str) and value and isinstance(concrete, str) and concrete:
            if (requested.endswith("[1m]") and not concrete.endswith("[1m]")) or (
                pinned and concrete != resolved
            ):
                matched = []
            else:
                label = option.get("display_name")
                unique_label = (
                    isinstance(label, str)
                    and sum(item.get("display_name") == label for item in candidates) == 1
                )
                return _Target(
                    value,
                    concrete,
                    label if unique_label else None,
                    option.get("provenance") == "structured",
                )
    # Old bridges can select an exact canonical row, never a guessed family label.
    if options is None and resolved in values and base not in pins and resolved == requested:
        return _Target(resolved, resolved, None, False)
    raise ClaudeModelSwitchRefused(
        f"This Claude terminal has no unambiguous session-only picker choice for {requested}. "
        "Refresh its model options or choose a model offered by the terminal.",
        unsupported=True,
    )


class SessionOnlyModelSwitch:
    """Own one picker and one fresh status overlay under the bridge's locks."""

    def __init__(
        self,
        bridge_dir: Path,
        *,
        model: str,
        deadline: ModelSwitchDeadline,
        ready_timeout_s: float | None,
        owned_draft: str | None,
    ) -> None:
        from omnigent.harnesses.claude_native import bridge

        self.bridge = bridge
        self.bridge_dir = bridge_dir
        self.model = model
        self.deadline = deadline
        self.ready_timeout_s = ready_timeout_s
        self.owned_draft = owned_draft
        self.phase = "preflight"
        self.submitted = False
        self.control_attempted = False
        self.commit_attempted = False
        self.info: dict[str, str] = {}
        self.active_session: str | None = None
        self.session = ""
        self.version = ""
        self.choice = _Target("", "", None, False)
        self.command_draft: str | None = None
        self.owned_picker: _Picker | None = None
        self.owned_status: dict[str, str] | None = None
        self.status_close_attempted = False

    def refuse(self, message: str, *, unsupported: bool = False) -> ClaudeModelSwitchRefused:
        return ClaudeModelSwitchRefused(
            message,
            phase=self.phase,
            command_submitted=self.submitted,
            model_may_have_changed=self.commit_attempted,
            unsupported=unsupported,
        )

    def check(self) -> None:
        self.bridge._check_injection_cancelled()
        if self.info and self.bridge.read_tmux_target(self.bridge_dir) != self.info:
            raise self.refuse(
                "The Claude terminal changed during model selection. Retry explicitly."
            )
        if self.bridge.read_active_session_id(self.bridge_dir) != self.active_session:
            raise self.refuse(
                "The active session changed during model selection. Retry explicitly."
            )
        current = self.bridge.read_claude_session_id(self.bridge_dir)
        if self.session and current != self.session:
            raise self.refuse(
                "The Claude session changed during model selection. Retry explicitly."
            )

    def pane(self) -> str:
        self.check()
        pane = self.bridge._capture_pane(self.info["socket_path"], self.info["tmux_target"])
        self.check()
        self.bridge._raise_if_user_prompt_pending(
            self.bridge_dir, pane, nothing_typed=not self.control_attempted
        )
        return pane

    def send(self, key: str, *, literal: bool = False) -> None:
        self.check()
        args = ["send-keys", *(["-l"] if literal else []), "-t", self.info["tmux_target"], key]
        self.control_attempted = True
        self.bridge._run_tmux(self.info["socket_path"], *args)

    def wait(self, accept: Callable[[str], bool], *, seconds: float | None = None) -> str:
        until = self.deadline.at
        if seconds is not None:
            until = min(until, self.deadline.clock() + seconds)
        while True:
            pane = self.pane()
            if accept(pane):
                return pane
            if self.deadline.clock() >= until:
                raise self.refuse(
                    "Claude did not show the expected immediate session-only model control. "
                    "The command may be queued or a dialog may be open. Check the terminal "
                    "before retrying; the dependent prompt was not sent."
                )
            self.bridge.time.sleep(min(0.05, max(0.0, until - self.deadline.clock())))

    def empty(self) -> str:
        pane = self.pane()
        if _draft(pane) != "":
            raise self.refuse("Claude's input box is occupied or clipped. Clear it, then retry.")
        return pane

    def command(self, command: str) -> None:
        self.empty()
        self.command_draft = command
        self.send(command, literal=True)
        self.wait(lambda pane: _draft(pane) == command, seconds=1.0)
        if _draft(self.pane()) != command:
            raise self.refuse(
                "The model control's draft changed before submission. Retry explicitly."
            )
        if command == "/model":
            self.submitted = True
        self.send("Enter")

    def clear_owned_draft(self, expected: str) -> None:
        pane = self.pane()
        if _draft(pane) != expected:
            raise self.refuse(
                "Claude's input draft changed. Inspect the terminal before retrying."
            )
        box = self.bridge._composer_box(pane)
        assert box is not None and box.framed
        self.check()
        # One verified burst only; changed or partially cleared drafts get no retry.
        self.control_attempted = True
        self.bridge._send_composer_clear_keys(
            self.info["socket_path"], self.info["tmux_target"], rows=len(box.rows)
        )
        self.wait(lambda text: _draft(text) == "", seconds=1.0)

    def cleanup_owned_control(self) -> None:
        if not self.info:
            return
        if not (self.command_draft or self.owned_picker or self.owned_status):
            return
        original_deadline = self.deadline
        self.deadline = ModelSwitchDeadline(1.0, None, original_deadline.clock)
        try:
            with self.bridge.cancellable_injection(self.deadline):
                self._cleanup_owned_control()
        finally:
            self.deadline = original_deadline

    def _cleanup_owned_control(self) -> None:
        with contextlib.suppress(RuntimeError, OSError, ValueError):
            pane = self.pane()
            if self.command_draft and _draft(pane) == self.command_draft:
                self.clear_owned_draft(self.command_draft)
            elif (
                self.owned_picker
                and not self.commit_attempted
                and _picker(pane) == self.owned_picker
            ):
                self.send("Escape")
                self.wait(lambda text: _draft(text) == "" and _picker(text) is None, seconds=1.0)
            elif (
                self.owned_status
                and not self.status_close_attempted
                and _status(pane) == self.owned_status
            ):
                self.status_close_attempted = True
                self.send("Escape")
                self.wait(lambda text: _draft(text) == "" and _status(text) is None, seconds=1.0)

    def status_matches(self, actual: str, *, exact_context: bool) -> bool:
        expected = {self.choice.model}
        canonical = _known_claude_id(self.choice.model)
        if canonical:
            expected.add(canonical)
        if actual in expected:
            return True
        if not exact_context and any(
            value.endswith("[1m]") and actual == value.removesuffix("[1m]") for value in expected
        ):
            return True
        return bool(
            not exact_context
            and self.choice.structured
            and self.choice.label
            and actual == self.choice.label
        )

    def local_status(self) -> str:
        self.phase = "status"
        self.command("/status")
        pane = self.wait(lambda text: _status(text) is not None, seconds=2.0)
        fields = _status(pane)
        assert fields is not None
        if fields["Session ID"] != self.session or fields["Version"] != self.version:
            raise self.refuse("The status view belongs to a different Claude session or version.")
        self.command_draft = None
        self.owned_status = fields
        latest = _status(self.pane())
        if latest != fields:
            raise self.refuse("Claude's status view changed before it could be confirmed.")
        self.status_close_attempted = True
        self.send("Escape")
        self.wait(lambda text: _draft(text) == "" and _status(text) is None, seconds=1.0)
        self.owned_status = None
        self.status_close_attempted = False
        return fields["Model"]

    def row_matches(self, row: _PickerRow) -> bool:
        if re.match(r"Default(?:\s|\()", row.text):
            return False
        if re.search(r"\b(?:disabled|unavailable)\b", row.text, re.IGNORECASE):
            return False
        if any(f"({value})" in row.text for value in {self.choice.model, self.choice.value}):
            return True
        if "Managed by your organization" in row.text:
            return False
        label = re.split(r"\s{2,}", row.text, maxsplit=1)[0].replace("✔", "").strip()
        if label in {self.choice.value, self.choice.model}:
            return True
        return bool(self.choice.structured and self.choice.label and label == self.choice.label)

    def result(
        self, status_model: str, *, already_selected: bool = False
    ) -> ClaudeModelSwitchResult:
        return ClaudeModelSwitchResult(
            self.model,
            self.choice.value,
            self.choice.model,
            status_model,
            self.session,
            already_selected,
        )

    def run(self) -> ClaudeModelSwitchResult:
        try:
            return self._run()
        except ClaudeModelSwitchRefused:
            self.cleanup_owned_control()
            raise
        except (RuntimeError, OSError, ValueError) as exc:
            if self.phase == "preflight" and not self.submitted and not self.control_attempted:
                if (
                    (isinstance(exc, self.bridge.ClaudeUserPromptPending) and exc.nothing_typed)
                    or isinstance(exc, self.bridge.ClaudeInputBoxOccupied)
                    or (isinstance(exc, self.bridge.ClaudeTerminalDialog) and exc.awaits_answer)
                    or isinstance(exc, self.bridge.ClaudeComposerNotCleared)
                ):
                    raise
            self.cleanup_owned_control()
            raise self.refuse(
                "Claude could not finish the session-only model selection. Inspect the terminal "
                "before retrying; the dependent prompt was not sent."
            ) from exc

    def _run(self) -> ClaudeModelSwitchResult:
        self.choice = _target(
            self.model,
            self.bridge.read_model_picker_options(self.bridge_dir),
            self.bridge.read_model_picker_values(self.bridge_dir),
            self.bridge.read_model_env(self.bridge_dir),
        )
        self.active_session = self.bridge.read_active_session_id(self.bridge_dir)
        if self.active_session is None:
            raise self.refuse(
                "This Claude terminal has no known owning session. Reopen it, then retry."
            )
        self.info = self.bridge._wait_for_tmux_info(
            self.bridge_dir, timeout_s=self.deadline.remaining_s()
        )
        self.session = self.bridge.read_claude_session_id(self.bridge_dir) or ""
        raw: dict[str, object] = {}

        def ready(pane: str) -> bool:
            nonlocal raw
            raw = self.bridge._read_json_file(self.bridge_dir / self.bridge.CONTEXT_RAW_FILE) or {}
            version = raw.get("version")
            session = raw.get("session_id")
            if isinstance(version, str) and isinstance(session, str):
                hook_session = self.bridge.read_claude_session_id(self.bridge_dir)
                if not hook_session or hook_session != session:
                    return False
                parsed = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
                if parsed is None or tuple(map(int, parsed.groups())) < _MIN_PICKER_VERSION:
                    raise self.refuse(
                        "Session-only model switching requires Claude Code 2.1.323 or newer. "
                        "Update Claude Code and relaunch this terminal.",
                        unsupported=True,
                    )
                self.session, self.version = session, version
                return _draft(pane) is not None
            return False

        self.wait(ready, seconds=self.ready_timeout_s)
        pane = self.pane()
        draft = _draft(pane)
        if draft:
            if self.owned_draft is None or draft != self.owned_draft:
                raise self.refuse(
                    "An unrelated draft occupies Claude's input box. Finish it, then retry."
                )
            self.clear_owned_draft(self.owned_draft)
        raw_model = raw.get("model")
        hint = raw_model.get("id") if isinstance(raw_model, dict) else raw_model
        if isinstance(hint, str) and self.status_matches(hint, exact_context=True):
            current = self.local_status()
            if self.status_matches(current, exact_context=True):
                return self.result(current, already_selected=True)

        self.phase = "picker"
        self.command("/model")
        pane = self.wait(
            lambda text: _picker(text) is not None, seconds=_IMMEDIATE_PICKER_TIMEOUT_S
        )
        visited: set[tuple[int, str]] = set()
        while True:
            picker = _picker(pane)
            assert picker is not None
            self.command_draft = None
            self.owned_picker = picker
            selected = picker.selected
            targets = [row for row in picker.rows if self.row_matches(row)]
            if len(targets) > 1:
                raise self.refuse(
                    "The live picker has ambiguous duplicate model rows. Retry explicitly."
                )
            if self.row_matches(selected):
                latest = _picker(self.pane())
                if latest is None or latest.selected != selected:
                    raise self.refuse(
                        "The highlighted model changed before selection. Retry explicitly."
                    )
                self.phase = "commit"
                self.commit_attempted = True
                self.send("s", literal=True)
                break
            identity = (selected.number, selected.text)
            if identity in visited or len(visited) >= 128:
                raise self.refuse(f"The live picker does not offer the exact model {self.model}.")
            visited.add(identity)
            direction = (
                "Up" if len(targets) == 1 and targets[0].number < selected.number else "Down"
            )
            self.send(direction)
            pane = self.wait(
                lambda text, previous=selected: (
                    (new := _picker(text)) is not None and new.selected != previous
                ),
                seconds=1.0,
            )
        self.wait(lambda text: _draft(text) == "" and _picker(text) is None, seconds=2.0)
        current = self.local_status()
        if not self.status_matches(current, exact_context=False):
            raise self.refuse(
                f"Claude's fresh status reports {current}, so the switch to {self.model} "
                "is unconfirmed. The dependent prompt was not sent."
            )
        return self.result(current)
