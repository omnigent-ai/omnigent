"""Editable Claude terminal peers shared by adapter and process tests."""

from pathlib import Path

_RULE = "──────────────────────────────"
_CTRL_U = "\x1b[117;5u"
_CTRL_K = "\x1b[107;5u"


class InputBox:
    """
    Claude Code's input box as far as shell mode is concerned.

    :param mode: ``"bash"`` for shell mode, ``"prompt"`` for chat input.
    :param draft: Text in the box.
    :param cursor: Cursor offset; ``None`` places it at the end.
    """

    def __init__(self, mode: str, draft: str, *, cursor: int | None = None) -> None:
        self.mode = mode
        self.draft = draft
        self.cursor = len(draft) if cursor is None else cursor
        self.submitted: list[tuple[str, str]] = []

    def pane(self) -> str:
        glyph = "!" if self.mode == "bash" else "❯"
        footer = "  ! for shell mode" if self.mode == "bash" else "  ? for shortcuts"
        first, *rest = self.draft.split("\n")
        rows = [f"{glyph} {first}", *(f"  {row}" for row in rest)]
        return "\n".join([_RULE, *rows, _RULE, footer]) + "\n"

    def key(self, key: str) -> None:
        if key in (_CTRL_U, "C-u"):
            # Clears the line; at the start of the line it leaves shell mode.
            if self.cursor == 0:
                self.mode = "prompt"
            else:
                start = self.draft.rfind("\n", 0, self.cursor) + 1
                if start == self.cursor and self.cursor:
                    start -= 1
                self.draft = self.draft[:start] + self.draft[self.cursor :]
                self.cursor = start
        elif key == _CTRL_K:
            end = self.draft.find("\n", self.cursor)
            end = len(self.draft) if end == -1 else end
            if end == self.cursor and end < len(self.draft):
                end += 1
            self.draft = self.draft[: self.cursor] + self.draft[end:]
        elif key == "Escape":
            # Shell mode ends only with the cursor at the start of an empty draft.
            if self.mode == "bash" and not self.draft:
                self.mode = "prompt"
        elif key == "\x1b[97;5u":
            self.cursor = self.draft.rfind("\n", 0, self.cursor) + 1
        elif key in {"Space", " "}:
            self.draft = self.draft[: self.cursor] + " " + self.draft[self.cursor :]
            self.cursor += 1
        elif key == "BSpace" and self.cursor:
            self.draft = self.draft[: self.cursor - 1] + self.draft[self.cursor :]
            self.cursor -= 1
        elif key == "Enter":
            self.submitted.append((self.mode, self.draft.removesuffix("\n")))
            self.draft = ""
            self.cursor = 0


# The caller supplies an optional draft trigger and stdin stall duration.
FAKE_CLAUDE_TUI = r"""
import codecs, os, sys, termios, time, tty

sys.path.insert(0, __REPO_ROOT__)
from tests.harnesses.claude_native._terminal import InputBox

STALL_TRIGGER = sys.argv[1] if len(sys.argv) > 1 else None
STALL_S = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0


def main():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    box = InputBox("prompt", "")
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending = ""
    pasted = False
    stalled = False
    keys = ("\x1b[107;5u", "\x1b[117;5u", "\x1b[200~", "\x1b[201~")

    def render():
        transcript = "".join("sent: " + text + "\n" for _, text in box.submitted)
        sys.stdout.write("\x1b[2J\x1b[H" + (transcript + box.pane()).replace("\n", "\r\n"))
        cursor_row = len(transcript.splitlines()) + 1 + box.draft[:box.cursor].count("\n")
        cursor_column = len(box.draft[:box.cursor].rsplit("\n", 1)[-1]) + 2
        sys.stdout.write(f"\x1b[{cursor_row + 1};{cursor_column + 1}H")
        sys.stdout.flush()

    sys.stdout.write("\x1b[?2004h")
    render()
    try:
        while True:
            data = os.read(fd, 65536)
            if not data:
                break
            pending += decoder.decode(data)
            while pending:
                token = next((key for key in keys if pending.startswith(key)), None)
                if token is not None:
                    pending = pending[len(token):]
                    if token == "\x1b[200~":
                        pasted = True
                    elif token == "\x1b[201~":
                        pasted = False
                    else:
                        box.key(token)
                elif any(key.startswith(pending) for key in keys):
                    break
                else:
                    ch, pending = pending[0], pending[1:]
                    if ch == "\x03":
                        return
                    if ch in {"\x7f", "\b"} and not pasted:
                        box.key("BSpace")
                    elif ch == "\r" and not pasted:
                        if box.draft.strip():
                            box.key("Enter")
                    elif ch >= " " or (pasted and ch in "\r\n"):
                        ch = "\n" if ch == "\r" else ch
                        box.draft = box.draft[:box.cursor] + ch + box.draft[box.cursor:]
                        box.cursor += 1
                render()
                if not stalled and STALL_TRIGGER and STALL_TRIGGER in box.draft:
                    stalled = True
                    time.sleep(STALL_S)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


main()
""".replace("__REPO_ROOT__", repr(str(Path(__file__).resolve().parents[3])))
