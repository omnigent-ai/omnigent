"""Shared Claude-native composer and model-selection emulators."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.claude_native.bridge import write_tmux_target

# The clear sent to a one-row input box: two Ctrl+K, then two Ctrl+U (CSI-u).
_CLEAR_ONE_ROW_COMPOSER = "\x1b[107;5u" * 2 + "\x1b[117;5u" * 2


def _picker_bridge_dir(tmp_path: Path) -> Path:
    """
    Create a bridge dir advertising a tmux pane.

    :param tmp_path: Per-test temp directory.
    :returns: The bridge directory.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/example/tmux.sock"),
        tmux_target="claude:0.0",
    )
    return bridge_dir


class _VirtualClock:
    """Clock whose ``sleep`` advances ``monotonic`` instead of blocking.

    The dialog-confirm poll runs off ``monotonic``, so a no-op ``sleep``
    alone would leave it spinning for its whole real-time budget.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now


class _EditableComposer:
    """Claude Code's input box, editing a multi-line draft as the real one does.

    Measured on Claude Code 2.1.287: Ctrl+K deletes to the end of the line,
    or the line break when the cursor is already there; Ctrl+U does the same
    towards the start; Ctrl+A moves to the start of the line. Each line of
    the draft renders as its own row of the box.
    """

    _RULE = "──────────────────────────────"

    def __init__(
        self,
        draft: str,
        *,
        cursor: int,
        visible_rows: int | None = None,
        pane_rows: int | None = None,
        unavailable_clear_captures: int = 0,
    ) -> None:
        """
        :param draft: Text the person left in the box.
        :param cursor: Cursor offset into *draft*.
        :param visible_rows: When set, the pane is a sliver with room for this
            many lines under the box's opening rule. A draft that fits with
            the closing rule shows framed; a taller one is cut off at the
            bottom of the pane without it (the ``_SLIVER_MULTILINE_DRAFT_PANE``
            geometry). ``None`` shows the whole box.
        :param pane_rows: When set, the pane has this many lines and keeps the
            bottom of the screen in view, as Claude Code 2.1.309 does: a draft
            too tall for it scrolls the opening rule and glyph row off the top.
        :param unavailable_clear_captures: Captures that fail after clearing
            starts; readiness captures remain readable.
        """
        self.text = draft
        self.cursor = cursor
        self._visible_rows = visible_rows
        self._pane_rows = pane_rows
        self._unavailable_clear_captures = unavailable_clear_captures
        self._clearing_started = False
        self.submitted: list[str] = []
        self._paste_buffer = ""
        self._actions = {
            "\x1b[107;5u": self._kill_to_line_end,
            "\x1b[117;5u": self._kill_to_line_start,
            "\x1b[97;5u": self._move_to_line_start,
        }

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Route the bridge's tmux calls to this box."""
        monkeypatch.setattr(claude_native_bridge, "_run_tmux", self._run_tmux)
        monkeypatch.setattr(claude_native_bridge, "_capture_pane", self._capture_pane)
        monkeypatch.setattr(claude_native_bridge, "time", _VirtualClock())
        # No Claude Code process runs behind this box for a repaint signal to reach.
        monkeypatch.setattr(claude_native_bridge, "_request_claude_repaint", lambda *_: 0)

    def _run_tmux(self, socket_path: str, *args: str) -> None:
        del socket_path
        if args[0] == "load-buffer":
            self._paste_buffer = Path(args[-1]).read_bytes().decode().replace("\r", "\n")
        elif args[0] == "paste-buffer":
            self._insert(self._paste_buffer)
        elif args[0] == "send-keys" and "-l" in args:
            keys = args[-1]
            while keys:
                sequence = next((seq for seq in self._actions if keys.startswith(seq)), None)
                if sequence is None:
                    self._insert(keys)
                    break
                self._actions[sequence]()
                keys = keys[len(sequence) :]
        elif args[0] == "send-keys" and args[-1] == "Space":
            self._clearing_started = True
            self._insert(" ")
        elif args[0] == "send-keys" and args[-1] == "Enter":
            self.submitted.append(self.text)
            self.text, self.cursor = "", 0

    def _capture_pane(self, socket_path: str, tmux_target: str) -> str:
        del socket_path, tmux_target
        if self._clearing_started and self._unavailable_clear_captures:
            self._unavailable_clear_captures -= 1
            return ""
        first, *rest = self.text.split("\n")
        rows = [f"❯ {first}", *(f"  {row}" for row in rest)]
        if self._visible_rows is not None and len(rows) >= self._visible_rows:
            # The rows take every line the pane has: no closing rule.
            clipped = "\n".join(rows[: self._visible_rows])
            return f"⏺ done\n{self._RULE}\n{clipped}\n"
        screen = ["⏺ done", self._RULE, *rows, self._RULE, "  ? for shortcuts"]
        if self._pane_rows is not None:
            screen = screen[-self._pane_rows :]
        return "\n".join(screen[1:] if self._pane_rows is None else screen) + "\n"

    def _insert(self, text: str) -> None:
        self.text = self.text[: self.cursor] + text + self.text[self.cursor :]
        self.cursor += len(text)

    def _kill_to_line_end(self) -> None:
        if self.text[self.cursor : self.cursor + 1] == "\n":
            end = self.cursor + 1
        else:
            end = self.text.find("\n", self.cursor)
            end = len(self.text) if end == -1 else end
        self.text = self.text[: self.cursor] + self.text[end:]

    def _kill_to_line_start(self) -> None:
        if self.text[self.cursor - 1 : self.cursor] == "\n":
            start = self.cursor - 1
        else:
            start = self.text.rfind("\n", 0, self.cursor) + 1
        self.text = self.text[:start] + self.text[self.cursor :]
        self.cursor = start

    def _move_to_line_start(self) -> None:
        self.cursor = self.text.rfind("\n", 0, self.cursor) + 1


def _cache_cost_confirmation_pane(label: str) -> str:
    return (
        "   Switch model?\n"
        "   Your next response will be slower and use more tokens\n\n"
        "   This conversation is cached for the current model. "
        f"Switching to {label} means the full history gets re-read.\n\n"
        f"   ❯ 1. Yes, switch to {label}\n"
        "     2. No, go back\n"
    )


_CAPTURED_DATED_MODEL_CONFIRMATION = (
    "─────────────────────────────────────────────────────────────────────────────────────────────────────────\n"
    "  Switch model?\n"
    "  Your next response will be slower and use more tokens\n\n"
    "  This conversation is cached for the current model. Switching to Sonnet 4 means "
    "the full history gets\n"
    "  re-read.\n\n"
    "  ❯ 1. Yes, switch to Sonnet 4\n"
    "    2. No, go back\n\n\n\n"
)


class _SessionModelComposer(_EditableComposer):
    """Editable composer with the recorded local model and status overlays."""

    def __init__(
        self,
        *,
        draft: str = "",
        busy: bool = False,
        prompt_separator: str = " ",
        cache_cost_label: str | None = None,
    ) -> None:
        super().__init__(draft, cursor=len(draft))
        self.options = ["claude-sonnet-5-5", "private-glm[1m]"]
        self.picker_labels: dict[str, str] = {}
        self.resolved_models: dict[str, str] = {}
        self.model = self.options[0]
        self.highlight = 0
        self.overlay: str | None = None
        self.busy = busy
        self.prompt_separator = prompt_separator
        self.sends: list[tuple[str, ...]] = []
        self.status_queries = 0
        self.session = "native-session"
        self.version = "2.1.323"
        self.cache_cost_label = cache_cost_label
        self.confirmations = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        super().install(monkeypatch)
        monkeypatch.setattr(
            claude_native_bridge,
            "_claude_pane_state",
            lambda *_: claude_native_bridge._ClaudePaneState(True),
        )

    def _run_tmux(self, socket_path: str, *args: str) -> None:
        claude_native_bridge._check_injection_cancelled()
        self.sends.append(args)
        key = args[-1]
        if self.overlay == "picker":
            if key == "Down":
                self.highlight = (self.highlight + 1) % len(self.options)
            elif key == "Up":
                self.highlight = (self.highlight - 1) % len(self.options)
            elif key == "s" and "-l" in args:
                if self.cache_cost_label:
                    self.overlay = "cache-cost"
                else:
                    self.model = self.resolved_models.get(
                        self.options[self.highlight], self.options[self.highlight]
                    )
                    self.overlay = None
            elif key == "Escape":
                self.overlay = None
            else:
                pytest.fail(f"Unexpected key in model picker: {args}")
            return
        if self.overlay == "cache-cost":
            assert key == "Enter", args
            self.confirmations += 1
            self.model = self.resolved_models.get(
                self.options[self.highlight], self.options[self.highlight]
            )
            self.overlay = None
            return
        if self.overlay == "status":
            assert key == "Escape", args
            self.overlay = None
            return
        command = self.text
        super()._run_tmux(socket_path, *args)
        if key == "Enter" and command == "/model":
            self.overlay = "picker"
        elif key == "Enter" and command == "/status":
            self.status_queries += 1
            self.overlay = "status"

    def _capture_pane(self, socket_path: str, tmux_target: str) -> str:
        if self.overlay == "cache-cost":
            assert self.cache_cost_label is not None
            return _cache_cost_confirmation_pane(self.cache_cost_label)
        if self.overlay == "picker":
            rows = "\n".join(
                f"   {'❯' if index == self.highlight else ' '} {index + 1}.  "
                f"{self.picker_labels.get(model, f'Choice {index}')}  "
                f"Managed by your organization ({model})"
                for index, model in enumerate(self.options)
            )
            return (
                "   Select model\n" + rows + "\n"
                "   Enter to set as default · s to use this session only · Esc to cancel\n"
            )
        if self.overlay == "status":
            return (
                "   Settings  Status  Config  Usage  Stats\n"
                f"   Version: {self.version}\n"
                f"   Session ID: {self.session}\n"
                f"   Model: {self.model}\n"
                "   Esc to cancel\n"
            )
        pane = super()._capture_pane(socket_path, tmux_target)
        pane = pane.replace("\n❯ ", f"\n❯{self.prompt_separator}", 1)
        return ("Working…\n" if self.busy else "") + pane


def _session_model_bridge(tmp_path: Path, composer: _SessionModelComposer) -> Path:
    bridge_dir = _picker_bridge_dir(tmp_path)
    (bridge_dir / "bridge.json").write_text(
        json.dumps(
            {
                "active_session_id": "web-session",
                "model_picker_values": composer.options,
                "model_picker_options": [
                    {
                        "value": model,
                        "model": composer.resolved_models.get(model, model),
                        "display_name": f"Choice {index}",
                        "enabled": True,
                        "provenance": "structured",
                    }
                    for index, model in enumerate(composer.options)
                ],
            }
        )
    )
    (bridge_dir / "state.json").write_text(json.dumps({"claude_session_id": composer.session}))
    (bridge_dir / claude_native_bridge.CONTEXT_RAW_FILE).write_text(
        json.dumps(
            {
                "session_id": composer.session,
                "version": composer.version,
                "model": {"id": composer.model},
            }
        )
    )
    return bridge_dir


def _versioned_cache_cost_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[_SessionModelComposer, Path]:
    composer = _SessionModelComposer(cache_cost_label="Sonnet 5.5")
    picker_value = "system.ai.claude-sonnet-5-5[1m]"
    composer.options = ["private-initial-model", picker_value]
    composer.model = composer.options[0]
    composer.picker_labels[picker_value] = "Sonnet"
    composer.resolved_models[picker_value] = "system.ai.claude-sonnet-5-5"
    bridge_dir = _session_model_bridge(tmp_path, composer)
    config_path = bridge_dir / "bridge.json"
    config = json.loads(config_path.read_text())
    config["model_picker_options"][1]["display_name"] = "Sonnet"
    config_path.write_text(json.dumps(config))
    composer.install(monkeypatch)
    return composer, bridge_dir


def _dated_cache_cost_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pane: str = _CAPTURED_DATED_MODEL_CONFIRMATION,
) -> tuple[_SessionModelComposer, Path]:
    composer = _SessionModelComposer(cache_cost_label="Sonnet 4")
    composer.options[1] = "sonnet"
    composer.picker_labels["sonnet"] = "Sonnet 4"
    composer.resolved_models["sonnet"] = "claude-sonnet-4-20250514"
    bridge_dir = _session_model_bridge(tmp_path, composer)
    config_path = bridge_dir / "bridge.json"
    config = json.loads(config_path.read_text())
    config["model_picker_options"][1]["display_name"] = "Sonnet 4"
    config["model_env"] = {"ANTHROPIC_DEFAULT_SONNET_MODEL": composer.resolved_models["sonnet"]}
    config_path.write_text(json.dumps(config))
    composer.install(monkeypatch)
    monkeypatch.setattr(
        claude_native_bridge,
        "_capture_pane",
        lambda socket_path, target: (
            pane
            if composer.overlay == "cache-cost"
            else composer._capture_pane(socket_path, target)
        ),
    )
    return composer, bridge_dir
