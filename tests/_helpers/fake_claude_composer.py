"""Input handling for the ASCII drafts in scripted Claude terminal fixtures."""

from inspect import getsource


class FakeClaudeComposer:
    """Consume editing keys and bracketed paste without showing escape bytes as text."""

    def __init__(self) -> None:
        self.chars: list[str] = []
        self.cursor = 0
        self.escape = bytearray()
        self.pasting = False

    @property
    def draft(self) -> str:
        return "".join(self.chars)

    def feed(self, data: bytes) -> str | None:
        """Consume one byte; return the submitted draft only for an unpasted Enter."""
        byte = data[0]
        if self.escape or byte == 27:
            self.escape.extend(data)
            if len(self.escape) == 1 or self.escape == b"\x1b[":
                return None
            if len(self.escape) > 32 or not self.escape.startswith(b"\x1b["):
                self.escape.clear()
                return None
            if not 0x40 <= byte <= 0x7E:
                return None
            sequence = bytes(self.escape)
            self.escape.clear()
            if sequence == b"\x1b[200~":
                self.pasting = True
            elif sequence == b"\x1b[201~":
                self.pasting = False
            else:
                self._edit(
                    {b"\x1b[97;5u": 1, b"\x1b[107;5u": 11, b"\x1b[117;5u": 21}.get(sequence)
                )
            return None
        if not self.pasting and byte in (1, 11, 21):
            self._edit(byte)
        elif byte == 13 and not self.pasting:
            submitted = self.draft
            self.chars.clear()
            self.cursor = 0
            return submitted or None
        elif byte >= 0x20 or (self.pasting and byte in (10, 13)):
            self.chars.insert(self.cursor, "\n" if byte in (10, 13) else chr(byte))
            self.cursor += 1
        return None

    def _edit(self, key: int | None) -> None:
        line_start = self.draft.rfind("\n", 0, self.cursor) + 1
        line_end = self.draft.find("\n", self.cursor)
        line_end = len(self.chars) if line_end < 0 else line_end
        if key == 1:
            self.cursor = line_start
        elif key == 11:
            end = line_end + (line_end == self.cursor and line_end < len(self.chars))
            del self.chars[self.cursor : end]
        elif key == 21:
            start = line_start - (line_start == self.cursor and self.cursor > 0)
            del self.chars[start : self.cursor]
            self.cursor = start


FAKE_CLAUDE_COMPOSER_SOURCE = getsource(FakeClaudeComposer)
