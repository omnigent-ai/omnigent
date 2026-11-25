import { EditorState, Text } from "@codemirror/state";
import { describe, expect, it, vi } from "vitest";
import {
  FILL_LINES_PER_FRAME,
  IndentationScopes,
  ScopeFiller,
  STICKY_SCROLL_MAX_LINES,
  type StickyScrollGeometry,
  stickyScrollLines,
} from "./indentationScopes";

const LINE_HEIGHT = 10;

/** Counts each line's indentation or parent link that `scopes` reads from now on. */
function countLinkReads(scopes: IndentationScopes): () => number {
  let reads = 0;
  const internals = scopes as unknown as Record<"indents" | "parents", number[]>;
  for (const field of ["indents", "parents"] as const) {
    const counted = new Proxy(internals[field], {
      get(target, key, receiver) {
        if (typeof key === "string" && /^\d+$/.test(key)) reads += 1;
        return Reflect.get(target, key, receiver);
      },
    });
    Object.defineProperty(scopes, field, { value: counted });
  }
  return () => reads;
}

/** Scopes with every link filled, as the shared filler leaves them once it catches up. */
function filledScopes(doc: Text, tabSize: number): IndentationScopes {
  const scopes = new IndentationScopes(doc, tabSize);
  scopes.fillToward(Infinity, Infinity);
  return scopes;
}

function docOf(lines: string[]): Text {
  return Text.of(lines);
}

/** Every line one `LINE_HEIGHT` tall, scrolled so `topLine` is the first line under the view's top. */
function geometryAt(
  doc: Text,
  topLine: number,
  { offset = 0, maxLines = STICKY_SCROLL_MAX_LINES } = {},
): StickyScrollGeometry {
  return {
    top: (topLine - 1) * LINE_HEIGHT + offset,
    lineHeight: LINE_HEIGHT,
    maxLines,
    lineCount: doc.lines,
    lineAtHeight: (height) => Math.min(doc.lines, Math.floor(height / LINE_HEIGHT) + 1),
    lineBottom: (lineNumber) => lineNumber * LINE_HEIGHT,
  };
}

const NESTED = [
  "class Outer {", // 1
  "  method() {", // 2
  "    if (ready) {", // 3
  "      first();", // 4
  "", // 5
  "      second();", // 6
  "      third();", // 7
  "      fourth();", // 8
  "    }", // 9
  "  }", // 10
  "}", // 11
  "after();", // 12
];

describe("IndentationScopes", () => {
  it("links each line to the lines whose blocks enclose it, outermost first", () => {
    const scopes = filledScopes(docOf(NESTED), 4);
    expect(scopes.enclosing(1)).toEqual([]);
    expect(scopes.enclosing(4)).toEqual([1, 2, 3]);
    expect(scopes.enclosing(9)).toEqual([1, 2]);
    expect(scopes.enclosing(12)).toEqual([]);
  });

  it("gives a blank line the scopes of the line above it", () => {
    const scopes = filledScopes(docOf(NESTED), 4);
    expect(scopes.indentation(5)).toBeNull();
    expect(scopes.enclosing(5)).toEqual([1, 2, 3]);
  });

  it("measures tabs to the next tab stop", () => {
    const scopes = filledScopes(docOf(["root:", "\tchild:", "  \t\tgrandchild"]), 4);
    expect(scopes.indentation(2)).toBe(4);
    expect(scopes.indentation(3)).toBe(8);
    expect(scopes.enclosing(3)).toEqual([1, 2]);
  });

  it("follows an edit above, and keeps answering for the lines before it", () => {
    const state = EditorState.create({ doc: docOf(NESTED) });
    const scopes = filledScopes(state.doc, state.tabSize);
    expect(scopes.enclosing(8)).toEqual([1, 2, 3]);
    // Unindenting the `if` makes it a sibling of `method`, no longer inside it.
    const line = state.doc.line(3);
    const edit = state.update({ changes: { from: line.from, to: line.from + 2, insert: "" } });
    scopes.update(edit.state.doc, edit.state.tabSize, edit.changes);
    // Links above the edit stay; the rest wait for the filler.
    expect(scopes.filledLines).toBe(2);
    expect(scopes.enclosing(2)).toEqual([1]);
    expect(scopes.enclosing(8)).toEqual([]);
    scopes.fillToward(Infinity, Infinity);
    expect(scopes.enclosing(8)).toEqual([1, 3]);
  });

  it("drops every link when the tab size changes", () => {
    const doc = docOf(["a:", "\tb", "  c"]);
    const scopes = filledScopes(doc, 4);
    expect(scopes.enclosing(3)).toEqual([1]);
    scopes.update(doc, 1, null);
    expect(scopes.filledLines).toBe(0);
    scopes.fillToward(Infinity, Infinity);
    // A tab is one column now, so `b` is shallower than `c` and opens its block.
    expect(scopes.enclosing(3)).toEqual([1, 2]);
  });

  it("answers for a line deep in a long file without walking back to its scope's start", () => {
    const body = Array.from({ length: 50_000 }, (_, index) => `    statement${index};`);
    const doc = docOf(["module {", "  section {", ...body, "  }", "}"]);
    const scopes = filledScopes(doc, 4);
    const reads = countLinkReads(scopes);
    expect(scopes.enclosing(40_000)).toEqual([1, 2]);
    for (let line = 40_001; line < 41_000; line += 1) scopes.enclosing(line);
    // Each answer is the line's own indentation and its two parent links, then the root.
    expect(reads()).toBeLessThanOrEqual(1_000 * 4);
  });

  it("answers for a line deep in a long blank run without walking back over it", () => {
    const blanks = Array.from({ length: 50_000 }, () => "    ");
    const doc = docOf(["module {", "  section {", "    statement;", ...blanks, "  }", "}"]);
    const scopes = filledScopes(doc, 4);
    const reads = countLinkReads(scopes);
    expect(scopes.enclosing(40_000)).toEqual([1, 2]);
    for (let line = 40_001; line < 41_000; line += 1) scopes.enclosing(line);
    // A blank line reads its nearest non-blank line, then that line's links, as above.
    expect(reads()).toBeLessThanOrEqual(1_000 * 6);
  });
});

describe("stickyScrollLines", () => {
  const doc = docOf(NESTED);

  it("pins nothing at the top of the file", () => {
    expect(stickyScrollLines(filledScopes(doc, 4), geometryAt(doc, 1))).toEqual({
      lines: [],
      pushUp: 0,
    });
  });

  it("doesn't pin a scope's first line while that line still shows at the top", () => {
    expect(stickyScrollLines(filledScopes(doc, 4), geometryAt(doc, 1, { offset: 4 }))).toEqual({
      lines: [],
      pushUp: 0,
    });
  });

  it("pins the scopes around the line under the pinned rows", () => {
    // Line 4 is under the top; one row covers it, so the scopes are line 5's, and so on.
    expect(stickyScrollLines(filledScopes(doc, 4), geometryAt(doc, 4)).lines).toEqual([1, 2, 3]);
    expect(stickyScrollLines(filledScopes(doc, 4), geometryAt(doc, 2)).lines).toEqual([1, 2, 3]);
  });

  it("pins at most the cap, keeping the outermost scopes", () => {
    const deep = docOf(
      Array.from({ length: 12 }, (_, depth) => `${" ".repeat(depth * 2)}level${depth}`),
    );
    const scopes = filledScopes(deep, 4);
    expect(stickyScrollLines(scopes, geometryAt(deep, 11)).lines).toEqual([1, 2, 3, 4, 5]);
    expect(stickyScrollLines(scopes, geometryAt(deep, 11, { maxLines: 2 })).lines).toEqual([1, 2]);
    expect(stickyScrollLines(scopes, geometryAt(deep, 11, { maxLines: 0 })).lines).toEqual([]);
  });

  it("slides the innermost row up as its scope's last line passes under it", () => {
    const scopes = filledScopes(doc, 4);
    // Rows for 1, 2 and 3 end at height 30 above line 7; the `if` block's last line is 8.
    const settled = stickyScrollLines(scopes, geometryAt(doc, 6));
    expect(settled).toEqual({ lines: [1, 2, 3], pushUp: 0 });
    const leaving = stickyScrollLines(scopes, geometryAt(doc, 6, { offset: 4 }));
    expect(leaving.lines).toEqual([1, 2, 3]);
    expect(leaving.pushUp).toBe(4);
  });

  it("drops the innermost row once its scope has scrolled away", () => {
    expect(stickyScrollLines(filledScopes(doc, 4), geometryAt(doc, 8)).lines).toEqual([1, 2]);
  });
});

function manualFrames() {
  let queued: (() => void)[] = [];
  return {
    request: (callback: () => void) => queued.push(callback),
    nextFrame: () => {
      const running = queued;
      queued = [];
      for (const callback of running) callback();
    },
  };
}

const listOf = (lines: number) =>
  docOf(["list:", ...Array.from({ length: lines }, (_, index) => `  item${index}`)]);
const noop = () => {};

describe("IndentationScopes reads", () => {
  it("answer only from filled links, and never fill", () => {
    const doc = docOf(NESTED);
    const scopes = new IndentationScopes(doc, 4);
    expect(scopes.enclosing(4)).toEqual([]);
    expect(scopes.indentation(4)).toBeNull();
    expect(stickyScrollLines(scopes, geometryAt(doc, 6)).lines).toEqual([]);
    expect(scopes.filledLines).toBe(0);
    expect(scopes.fillToward(4, 10)).toBe(4);
    expect(scopes.enclosing(4)).toEqual([1, 2, 3]);
  });
});

describe("ScopeFiller", () => {
  it("fills toward a line a slice per frame and reports each frame's progress", () => {
    const frames = manualFrames();
    const filler = new ScopeFiller(1_000, frames.request);
    const scopes = new IndentationScopes(listOf(2_500), 4);
    let progress = 0;
    filler.request(scopes, 2_000, () => (progress += 1));
    // Asking fills nothing; the frame does.
    expect(scopes.filledLines).toBe(0);
    frames.nextFrame();
    expect(scopes.filledLines).toBe(1_000);
    frames.nextFrame();
    expect(scopes.reaches(2_000)).toBe(true);
    expect(scopes.enclosing(2_000)).toEqual([1]);
    expect(progress).toBe(2);
    // Done: no further frame runs.
    frames.nextFrame();
    expect(progress).toBe(2);
  });

  it("starts each frame's slice inside a long blank run without walking back over it", () => {
    const frames = manualFrames();
    const filler = new ScopeFiller(1_000, frames.request);
    const blanks = Array.from({ length: 5_000 }, () => "");
    const scopes = new IndentationScopes(docOf(["a {", "  b {", "    c;", ...blanks, "  d;"]), 4);
    filler.request(scopes, 5_004, noop);
    frames.nextFrame();
    // The next slices start mid-run: reading where to resume costs a few links, not the run.
    const reads = countLinkReads(scopes);
    while (!scopes.reaches(5_004)) frames.nextFrame();
    const resumes = 5;
    expect(reads()).toBeLessThanOrEqual(4_004 * 2 + resumes * 8);
    expect(scopes.enclosing(5_004)).toEqual([1]);
    expect(scopes.enclosing(3_000)).toEqual([1, 2]);
  });

  it("fills at most one frame's lines however many passes ask in that frame", () => {
    const frames = manualFrames();
    const filler = new ScopeFiller(FILL_LINES_PER_FRAME, frames.request);
    const scopes = new IndentationScopes(listOf(100_000), 4);
    // A deep jump while the pane resizes: the view measures again before the frame ends.
    for (let pass = 0; pass < 4; pass += 1) filler.request(scopes, 90_000, noop);
    frames.nextFrame();
    expect(scopes.filledLines).toBe(FILL_LINES_PER_FRAME);
    for (let pass = 0; pass < 4; pass += 1) filler.request(scopes, 90_000, noop);
    frames.nextFrame();
    expect(scopes.filledLines).toBe(2 * FILL_LINES_PER_FRAME);
  });

  it("keeps one frame's total across editors whose measurements run before and after its run", () => {
    const frames = manualFrames();
    const filler = new ScopeFiller(FILL_LINES_PER_FRAME, frames.request);
    const left = new IndentationScopes(listOf(100_000), 4);
    const right = new IndentationScopes(listOf(100_000), 4);
    // A measurement queued before the filler's run, one that queues the run, and one after it.
    frames.request(() => filler.request(left, 90_000, noop));
    filler.request(right, 5_000, noop);
    frames.request(() => filler.request(right, 90_000, noop));
    frames.nextFrame();
    expect(left.filledLines + right.filledLines).toBeLessThanOrEqual(FILL_LINES_PER_FRAME);
    // Every frame after that stays within the cap too, until both catch up.
    let total = left.filledLines + right.filledLines;
    while (!left.reaches(90_000) || !right.reaches(90_000)) {
      frames.nextFrame();
      const now = left.filledLines + right.filledLines;
      expect(now - total).toBeGreaterThan(0);
      expect(now - total).toBeLessThanOrEqual(FILL_LINES_PER_FRAME);
      total = now;
    }
  });

  it("tells every view waiting on shared scopes, each once it reaches its own line", () => {
    const frames = manualFrames();
    const filler = new ScopeFiller(1_000, frames.request);
    // Two reviews of the same diff share its scopes.
    const scopes = new IndentationScopes(listOf(5_000), 4);
    const first: number[] = [];
    const second: number[] = [];
    filler.request(scopes, 1_500, () => first.push(scopes.filledLines));
    filler.request(scopes, 2_500, () => second.push(scopes.filledLines));
    frames.nextFrame();
    frames.nextFrame();
    frames.nextFrame();
    expect(first).toEqual([1_000, 2_000]);
    expect(second).toEqual([1_000, 2_000, 2_500]);
    // Both are done, so no further frame runs.
    frames.nextFrame();
    expect(second).toHaveLength(3);
  });

  it("keeps the other views' requests when one view closes", () => {
    const frames = manualFrames();
    const filler = new ScopeFiller(1_000, frames.request);
    const scopes = new IndentationScopes(listOf(5_000), 4);
    const closed = vi.fn();
    const open = vi.fn();
    filler.request(scopes, 1_500, closed);
    filler.request(scopes, 1_500, open);
    filler.cancel(scopes, closed);
    frames.nextFrame();
    expect(closed).not.toHaveBeenCalled();
    expect(open).toHaveBeenCalledOnce();
  });

  it("forgets a closed editor's request", () => {
    const frames = manualFrames();
    const filler = new ScopeFiller(1_000, frames.request);
    const scopes = new IndentationScopes(listOf(5_000), 4);
    filler.request(scopes, 4_000, noop);
    filler.cancel(scopes);
    frames.nextFrame();
    expect(scopes.filledLines).toBe(0);
  });
});
