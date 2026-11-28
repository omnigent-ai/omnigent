import { describe, expect, it } from "vitest";

import { comparison, identity } from "./stackDiffFixtures";
import {
  IDLE_STACK_COMPARISON,
  type StackComparisonEvent,
  type StackComparisonState,
  stackComparisonReducer,
} from "./stackComparisonState";

const run = (events: StackComparisonEvent[], from: StackComparisonState = IDLE_STACK_COMPARISON) =>
  events.reduce(stackComparisonReducer, from);

const accepted = () =>
  run([
    { type: "identity", identity: identity(), read: true },
    { type: "answer", identity: identity(), comparison: comparison() },
  ]);

describe("stackComparisonReducer", () => {
  it("reads what the page would compare, then accepts its own answer", () => {
    expect(run([{ type: "identity", identity: identity(), read: true }])).toEqual({
      kind: "reading",
      identity: identity(),
    });
    expect(accepted()).toEqual({
      kind: "accepted",
      identity: identity(),
      comparison: comparison(),
    });
  });

  it("drops an answer for another comparison than the one it's reading", () => {
    const reading = run([
      { type: "identity", identity: identity({ childHead: "e".repeat(40) }), read: true },
    ]);
    expect(
      stackComparisonReducer(reading, {
        type: "answer",
        identity: identity(),
        comparison: comparison(),
      }),
    ).toBe(reading);
  });

  it("reads the newer comparison when the PR moves before an answer came", () => {
    const moved = identity({ childHead: "e".repeat(40) });
    expect(
      run([
        { type: "identity", identity: identity(), read: true },
        { type: "identity", identity: moved, read: true },
      ]),
    ).toEqual({ kind: "reading", identity: moved });
  });

  for (const [name, change] of [
    ["the PR's push", { childHead: "e".repeat(40) }],
    ["the parent's push", { parentHead: "f".repeat(40) }],
    ["a description edit", { childBody: "Stacked on #99" }],
    ["another reader", { reader: '["mac-read-as","bob"]' }],
    ["the parent closing", { parentState: "CLOSED" }],
  ] as const) {
    it(`makes a shown comparison stale after ${name}, never another comparison`, () => {
      expect(
        stackComparisonReducer(accepted(), {
          type: "identity",
          identity: identity(change),
          read: true,
        }),
      ).toEqual({ kind: "stale" });
    });
  }

  it("makes a shown comparison stale when the page can't compare any more", () => {
    expect(
      stackComparisonReducer(accepted(), { type: "identity", identity: null, read: true }),
    ).toEqual({
      kind: "stale",
    });
  });

  it("keeps a shown comparison while the page still compares the same", () => {
    const state = accepted();
    expect(
      stackComparisonReducer(state, { type: "identity", identity: identity(), read: true }),
    ).toBe(state);
  });

  it("goes stale when the shell says the PR changed before it answered", () => {
    expect(
      run([
        { type: "identity", identity: identity(), read: true },
        { type: "failed", identity: identity(), changed: true },
      ]),
    ).toEqual({ kind: "stale" });
  });

  it("only leaves stale through a Reload that read the PR afresh, then compares again", () => {
    const later = identity({ parentHead: "f".repeat(40) });
    const stale = stackComparisonReducer(accepted(), {
      type: "identity",
      identity: later,
      read: true,
    });
    expect(
      run(
        [
          { type: "identity", identity: later, read: true },
          { type: "answer", identity: later, comparison: comparison() },
          { type: "failed", identity: later, changed: false },
          { type: "retry" },
        ],
        stale,
      ),
    ).toEqual({ kind: "stale" });
    // While Reload reads, nothing compares, so an out-of-date PR isn't compared again.
    const reloading = run(
      [{ type: "reload-started" }, { type: "identity", identity: identity(), read: true }],
      stale,
    );
    expect(reloading).toEqual({ kind: "reloading" });
    expect(
      run(
        [
          { type: "reload-settled", ok: true },
          { type: "identity", identity: later, read: true },
        ],
        reloading,
      ),
    ).toEqual({ kind: "reading", identity: later });
    // A Reload that couldn't read leaves Reload to try again.
    expect(stackComparisonReducer(reloading, { type: "reload-settled", ok: false })).toEqual({
      kind: "stale",
    });
  });

  it("makes a shown comparison stale on a change while Changes isn't open", () => {
    expect(
      stackComparisonReducer(accepted(), {
        type: "identity",
        identity: identity({ childHead: "e".repeat(40) }),
        read: false,
      }),
    ).toEqual({ kind: "stale" });
    // Without a change, closing Changes keeps it.
    const state = accepted();
    expect(
      stackComparisonReducer(state, { type: "identity", identity: identity(), read: false }),
    ).toBe(state);
  });

  it("asks a failed read again only when the person asks, and never one the shell can't do", () => {
    const failed = run([
      { type: "identity", identity: identity(), read: true },
      { type: "failed", identity: identity(), changed: false },
    ]);
    expect(
      run([{ type: "retry" }, { type: "identity", identity: identity(), read: true }], failed),
    ).toEqual({ kind: "reading", identity: identity() });
    const unsupported = run([
      { type: "identity", identity: identity(), read: true },
      { type: "answer", identity: identity(), comparison: null },
    ]);
    expect(stackComparisonReducer(unsupported, { type: "retry" })).toBe(unsupported);
  });

  it("tells a failed read from a shell that can't compare", () => {
    const reading = run([{ type: "identity", identity: identity(), read: true }]);
    expect(
      stackComparisonReducer(reading, { type: "failed", identity: identity(), changed: false }),
    ).toEqual({ kind: "failed", identity: identity(), unsupported: false });
    expect(
      stackComparisonReducer(reading, { type: "answer", identity: identity(), comparison: null }),
    ).toEqual({ kind: "failed", identity: identity(), unsupported: true });
  });

  it("doesn't read a failed comparison again until the PR or its parent moves", () => {
    const failed = run([
      { type: "identity", identity: identity(), read: true },
      { type: "failed", identity: identity(), changed: false },
    ]);
    expect(
      stackComparisonReducer(failed, { type: "identity", identity: identity(), read: true }),
    ).toBe(failed);
    expect(
      stackComparisonReducer(failed, {
        type: "identity",
        identity: identity({ childHead: "e".repeat(40) }),
        read: true,
      }).kind,
    ).toBe("reading");
  });
});

describe("stackComparisonReducer on a refresh", () => {
  it("starts over from any comparison or failure, so the refreshed inputs are compared anew", () => {
    const reading = run([{ type: "identity", identity: identity(), read: true }]);
    const failed = run([{ type: "failed", identity: identity(), changed: false }], reading);
    const unsupported = run([{ type: "answer", identity: identity(), comparison: null }], reading);
    const stale = run(
      [{ type: "identity", identity: identity({ childHead: "e".repeat(40) }), read: false }],
      accepted(),
    );
    const reloading = run([{ type: "reload-started" }], stale);
    for (const state of [accepted(), reading, failed, unsupported, stale, reloading]) {
      expect(stackComparisonReducer(state, { type: "refresh" })).toEqual(IDLE_STACK_COMPARISON);
    }
    expect(stackComparisonReducer(IDLE_STACK_COMPARISON, { type: "refresh" })).toBe(
      IDLE_STACK_COMPARISON,
    );
  });
});
