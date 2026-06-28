/* eslint-disable no-underscore-dangle -- tests reach into the addon's `_timeline`
   and xterm's `_core` to assert internal state; that introspection is the point. */
// Tests for the vendored VS Code type-ahead (predictive local echo) addon and
// its standalone shims. We exercise the addon against a real xterm Terminal in
// jsdom (the upstream code has no public unit surface, so we drive it through
// `term.onData` + the public `beforeServerInput` entry point and observe what
// it writes back to the terminal).
//
// Style note: predicted glyphs are wrapped in the "dim" SGR (`CSI 2 m` … `CSI
// 22 m`); we assert on those sequences to tell "a prediction was painted" from
// "nothing happened".

import { Terminal } from "@xterm/xterm";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  Color,
  DisposableStore,
  Emitter,
  RGBA,
  debounce,
  disposableTimeout,
  escapeRegExpCharacters,
  isNumber,
} from "./shims";
import { TypeAheadAddon, type ITypeAheadOptions } from "./terminalTypeAheadAddon";

// ---------------------------------------------------------------------------
// Shims
// ---------------------------------------------------------------------------

describe("shims", () => {
  it("Emitter delivers events and unsubscribes via the returned disposable", () => {
    const emitter = new Emitter<number>();
    const seen: number[] = [];
    const sub = emitter.event((n) => seen.push(n));
    emitter.fire(1);
    emitter.fire(2);
    sub.dispose();
    emitter.fire(3);
    expect(seen).toEqual([1, 2]);
  });

  it("DisposableStore disposes children once and short-circuits after disposal", () => {
    const store = new DisposableStore();
    const a = vi.fn();
    store.add({ dispose: a });
    store.dispose();
    store.dispose(); // idempotent
    expect(a).toHaveBeenCalledOnce();
    // adding after disposal disposes immediately
    const b = vi.fn();
    store.add({ dispose: b });
    expect(b).toHaveBeenCalledOnce();
  });

  it("disposableTimeout fires after the delay and can be cancelled", () => {
    vi.useFakeTimers();
    try {
      const fn = vi.fn();
      disposableTimeout(fn, 100);
      vi.advanceTimersByTime(99);
      expect(fn).not.toHaveBeenCalled();
      vi.advanceTimersByTime(1);
      expect(fn).toHaveBeenCalledOnce();

      const fn2 = vi.fn();
      disposableTimeout(fn2, 100).dispose();
      vi.advanceTimersByTime(200);
      expect(fn2).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("debounce collapses rapid calls to a single trailing invocation", () => {
    vi.useFakeTimers();
    try {
      const fn = vi.fn();
      const debounced = debounce(fn, 50);
      debounced();
      debounced();
      debounced();
      vi.advanceTimersByTime(49);
      expect(fn).not.toHaveBeenCalled();
      vi.advanceTimersByTime(1);
      expect(fn).toHaveBeenCalledOnce();
    } finally {
      vi.useRealTimers();
    }
  });

  it("escapeRegExpCharacters escapes regex metacharacters", () => {
    expect(escapeRegExpCharacters("a.b*c")).toBe("a\\.b\\*c");
    // used by the exclude-program list — a literal program name must match literally
    expect(new RegExp(escapeRegExpCharacters("g++")).test("g++")).toBe(true);
  });

  it("isNumber narrows only finite numeric values", () => {
    expect(isNumber(3)).toBe(true);
    expect(isNumber(NaN)).toBe(false);
    expect(isNumber("3")).toBe(false);
  });

  it("Color.fromHex parses #rrggbb and throws on garbage", () => {
    expect(new Color(new RGBA(1, 2, 3)).rgba.r).toBe(1);
    expect(Color.fromHex("#ff8800").rgba).toMatchObject({ r: 255, g: 136, b: 0 });
    expect(() => Color.fromHex("not-a-color")).toThrow();
  });
});

// ---------------------------------------------------------------------------
// Addon
// ---------------------------------------------------------------------------

/**
 * Build a real Terminal + activated addon. We don't call `term.open` (jsdom has
 * no canvas); the buffer/cursor APIs the addon reads work on an unopened
 * terminal. `writes` captures everything the addon paints back.
 */
function makeAddon(options?: Partial<ITypeAheadOptions>) {
  const term = new Terminal({ allowProposedApi: true, cols: 80, rows: 24 });
  const writes: string[] = [];
  const realWrite = term.write.bind(term);
  vi.spyOn(term, "write").mockImplementation((data: string | Uint8Array, cb?: () => void) => {
    writes.push(typeof data === "string" ? data : new TextDecoder().decode(data));
    return realWrite(data as string, cb);
  });

  const addon = new TypeAheadAddon({
    latencyThreshold: 0, // "on": show predictions immediately
    style: "dim",
    ...options,
  });
  addon.activate(term);
  return { term, addon, writes };
}

/** Flush xterm's async write queue so buffer state reflects what we wrote. */
function flush(term: Terminal): Promise<void> {
  return new Promise((resolve) => term.write("", () => resolve()));
}

const DIM = "\x1b[2m"; // CSI 2 m — the "apply" sequence for style: 'dim'

describe("TypeAheadAddon", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("holds the first char on a line tentative, then paints once the line is confirmed", async () => {
    const { term, addon, writes } = makeAddon();
    await flush(term);
    writes.length = 0;

    // First char on a fresh line is wrapped in a TentativeBoundary (epoch
    // model): it's queued for matching but NOT painted, so a no-echo context
    // (password prompt) never flickers.
    term.input("a");
    expect(writes.join("")).not.toContain(DIM);

    // Server confirms the first char → the line is proven to echo.
    addon.beforeServerInput("a");
    writes.length = 0;

    // Now subsequent chars paint immediately as dimmed (unconfirmed) glyphs.
    term.input("b");
    const painted = writes.join("");
    expect(painted).toContain(DIM);
    expect(painted).toContain("b");
  });

  it("does not predict in the alternate screen buffer (TUI like vim)", async () => {
    const { term, writes } = makeAddon();
    // Enter the alternate screen.
    await new Promise<void>((r) => term.write("\x1b[?1049h", () => r()));
    writes.length = 0;

    term.input("a");

    // No dim-styled prediction should be emitted on the alt screen.
    expect(writes.join("")).not.toContain(DIM);
  });

  it("beforeServerInput returns input unchanged when there are no predictions", () => {
    const { addon } = makeAddon();
    expect(addon.beforeServerInput("hello")).toBe("hello");
  });

  it("reconciles a correct echo (prediction confirmed, stats record success)", async () => {
    const { term, addon } = makeAddon();
    await flush(term);

    term.input("a");
    // The server echoes the same character back.
    addon.beforeServerInput("a");

    // A confirmed prediction is recorded as accurate.
    expect(addon.stats!.sampleSize).toBeGreaterThan(0);
    expect(addon.stats!.accuracy).toBe(1);
  });

  it("rolls back on a contradicting echo (prediction failed)", async () => {
    const { term, addon } = makeAddon();
    await flush(term);

    term.input("a");
    // Server echoes a DIFFERENT character — the prediction was wrong.
    addon.beforeServerInput("X");

    expect(addon.stats!.sampleSize).toBeGreaterThan(0);
    expect(addon.stats!.accuracy).toBe(0);
  });

  it("clears outstanding predictions after the timeout when no echo arrives", async () => {
    vi.useFakeTimers();
    try {
      const { term, addon } = makeAddon();
      term.write("");
      // Type two chars and confirm the first so the second is a live (painted)
      // prediction left outstanding.
      term.input("a");
      addon.beforeServerInput("a");
      term.input("b");
      const timeline = (addon as unknown as { _timeline: { length: number } })._timeline;
      expect(timeline.length).toBeGreaterThan(0);

      // No echo for "b". The deferred-clear timer (>=500ms) undoes predictions.
      vi.advanceTimersByTime(1000);
      expect(timeline.length).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("suppresses prediction when the terminal title matches an excluded program", async () => {
    const { term, writes } = makeAddon({
      excludePrograms: ["vim"],
      latencyThreshold: 0,
    });
    await flush(term);
    // Set the title to a vim session via OSC 0.
    await new Promise<void>((r) => term.write("\x1b]0;vim README.md\x07", () => r()));
    // Title change triggers a debounced re-evaluate; let it run.
    await new Promise((r) => setTimeout(r, 150));
    writes.length = 0;

    term.input("a");

    expect(writes.join("")).not.toContain(DIM);
  });

  it("disposes cleanly without leaking the rollback timer", () => {
    vi.useFakeTimers();
    try {
      const { term, addon } = makeAddon();
      term.write("");
      term.input("a"); // arms the deferred-clear timeout
      addon.dispose();
      // After dispose, the pending timer must not fire into a torn-down timeline.
      expect(() => vi.advanceTimersByTime(2000)).not.toThrow();
    } finally {
      vi.useRealTimers();
    }
  });

  it("regression guard: xterm still exposes the private _curAttrData the addon depends on", () => {
    const term = new Terminal({ allowProposedApi: true });
    // The addon reads `_core._inputHandler._curAttrData` for style rollback.
    const core = (term as unknown as { _core?: { _inputHandler?: { _curAttrData?: unknown } } })
      ._core;
    expect(core?._inputHandler?._curAttrData).toBeDefined();
    term.dispose();
  });
});
