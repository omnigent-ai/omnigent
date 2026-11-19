import { describe, expect, it } from "vitest";
import {
  EMPTY_KILL_RING,
  endKillRun,
  lineEndAfter,
  lineStartBefore,
  pushKill,
  shellWordStartBefore,
  yankText,
} from "./readline";

/** The offset of `|` in `marked`, and the text without it. */
function caret(marked: string): [string, number] {
  const offset = marked.indexOf("|");
  return [marked.slice(0, offset) + marked.slice(offset + 1), offset];
}

describe("readline boundaries", () => {
  it.each([
    ["git push origin|", "git push ".length],
    ["git push origin   |", "git push ".length],
    ["path/to/file.ts|", 0],
    ["one\ntwo|", "one\n".length],
    ["|start", 0],
  ])("⌃W from %j deletes back to the previous whitespace", (marked, expected) => {
    expect(shellWordStartBefore(...caret(marked))).toBe(expected);
  });

  it("⌃U kills to the line start, and across the break at the start of a line", () => {
    expect(lineStartBefore(...caret("one\ntwo th|ree"))).toBe("one\n".length);
    expect(lineStartBefore(...caret("one\n|two"))).toBe("one".length);
    expect(lineStartBefore(...caret("|one"))).toBe(0);
  });

  it("⌃K kills to the line end, and across the break at the end of a line", () => {
    expect(lineEndAfter(...caret("on|e\ntwo"))).toBe("one".length);
    expect(lineEndAfter(...caret("one|\ntwo"))).toBe("one\n".length);
    expect(lineEndAfter(...caret("one|"))).toBe("one".length);
  });
});

describe("kill ring", () => {
  it("joins consecutive backward kills in reading order", () => {
    let ring = pushKill(EMPTY_KILL_RING, "push ", true);
    ring = pushKill(ring, "git ", true);
    expect(yankText(ring)).toBe("git push ");
    expect(ring.entries).toHaveLength(1);
  });

  it("joins consecutive forward kills after the newest entry", () => {
    let ring = pushKill(EMPTY_KILL_RING, "one", false);
    ring = pushKill(ring, "\n", false);
    expect(yankText(ring)).toBe("one\n");
  });

  it("starts a new entry once an edit or caret move ends the run", () => {
    let ring = pushKill(EMPTY_KILL_RING, "first", true);
    ring = endKillRun(ring);
    ring = pushKill(ring, "second", true);
    expect(ring.entries).toEqual(["second", "first"]);
  });

  it("keeps at most 30 entries, dropping the oldest", () => {
    let ring = EMPTY_KILL_RING;
    for (let index = 0; index < 35; index += 1) {
      ring = pushKill(endKillRun(ring), `kill ${index}`, true);
    }
    expect(ring.entries).toHaveLength(30);
    expect(ring.entries[0]).toBe("kill 34");
    expect(ring.entries.at(-1)).toBe("kill 5");
  });

  it("yanks nothing before any kill", () => {
    expect(yankText(EMPTY_KILL_RING)).toBeNull();
  });
});
