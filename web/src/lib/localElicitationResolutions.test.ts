import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import {
  ACK_SETTLE_MS,
  NATIVE_SETTLE_MS,
  SUBMIT_TIMEOUT_MS,
  acknowledgeLocalElicitationResolution,
  affectedSessionIds,
  applyLocalElicitationResolutions,
  beginLocalElicitationResolution,
  cancelLocalElicitationResolution,
  getLocalElicitationResolutions,
  observeSessionPendingCounts,
  resetLocalElicitationResolutionsForTests,
} from "./localElicitationResolutions";

function row(id: string, pendingCount: number | undefined): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 1,
    updated_at: 1,
    labels: {},
    permission_level: 4,
    pending_elicitations_count: pendingCount,
  };
}

function currentResolutions() {
  return getLocalElicitationResolutions();
}

/** The displayed count per session id, as the sidebar derives it. */
function displayedCounts(rows: Conversation[]): Record<string, number> {
  const adjusted = applyLocalElicitationResolutions(rows, currentResolutions());
  return Object.fromEntries(
    adjusted.map((item) => [item.id, item.pending_elicitations_count ?? 0]),
  );
}

const NO_PARENT = () => null;

describe("local elicitation resolutions", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    resetLocalElicitationResolutionsForTests();
    vi.useRealTimers();
  });

  it("subtracts a held answer from the count of the session it belongs to", () => {
    observeSessionPendingCounts([row("sess_1", 1), row("sess_2", 1)]);
    beginLocalElicitationResolution("elicit_a", ["sess_1"], { nativeDelivery: false });

    expect(displayedCounts([row("sess_1", 1), row("sess_2", 1)])).toEqual({
      sess_1: 0,
      sess_2: 1,
    });
    expect(currentResolutions().answeredElicitationIds.has("elicit_a")).toBe(true);
  });

  it("does not subtract an answer the server already counted out before the drop is observed", () => {
    observeSessionPendingCounts([row("sess_1", 2)]);
    beginLocalElicitationResolution("elicit_a", ["sess_1"], { nativeDelivery: false });
    expect(displayedCounts([row("sess_1", 2)])).toEqual({ sess_1: 1 });

    // The server count reached the sidebar before its drop was observed.
    expect(displayedCounts([row("sess_1", 1)])).toEqual({ sess_1: 1 });
  });

  it("does not count one observed drop against two held answers", () => {
    observeSessionPendingCounts([row("sess_1", 2)]);
    const a = beginLocalElicitationResolution("elicit_a", ["sess_1"], { nativeDelivery: false });
    const b = beginLocalElicitationResolution("elicit_b", ["sess_1"], { nativeDelivery: false });
    acknowledgeLocalElicitationResolution("elicit_a", a);
    acknowledgeLocalElicitationResolution("elicit_b", b);
    expect(displayedCounts([row("sess_1", 2)])).toEqual({ sess_1: 0 });
    expect(displayedCounts([row("sess_1", 1)])).toEqual({ sess_1: 0 });

    observeSessionPendingCounts([row("sess_1", 1)]);

    expect(displayedCounts([row("sess_1", 1)])).toEqual({ sess_1: 0 });
    // The drop releases the older answer.
    expect([...currentResolutions().answeredElicitationIds]).toEqual(["elicit_b"]);
  });

  it("clears the terminal block only when a held answer clears the last counted prompt", () => {
    observeSessionPendingCounts([row("sess_1", 1), row("sess_2", 1)]);
    beginLocalElicitationResolution("elicit_a", ["sess_1"], { nativeDelivery: false });
    beginLocalElicitationResolution("elicit_b", ["sess_2"], { nativeDelivery: false });
    const parked = {
      ...row("sess_1", 1),
      status: "running" as const,
      blocked_on: "permission prompt",
    };
    // The server already counts this answer out; the terminal is parked on something else.
    const reparked = { ...row("sess_2", 0), status: "running" as const, blocked_on: "dialog open" };
    const [held, untouched] = applyLocalElicitationResolutions(
      [parked, reparked],
      currentResolutions(),
    );
    expect(held.pending_elicitations_count).toBe(0);
    expect(held.blocked_on).toBeUndefined();
    expect(untouched).toBe(reparked);
  });

  it("never shows a negative count", () => {
    observeSessionPendingCounts([row("sess_1", 1)]);
    beginLocalElicitationResolution("elicit_a", ["sess_1"], { nativeDelivery: false });
    beginLocalElicitationResolution("elicit_b", ["sess_1"], { nativeDelivery: false });

    expect(displayedCounts([row("sess_1", 1)])).toEqual({ sess_1: 0 });
  });

  it("returns the same array and row objects when no row needs an adjustment", () => {
    const rows = [row("sess_1", 1), row("sess_2", 0)];
    observeSessionPendingCounts(rows);
    expect(applyLocalElicitationResolutions(rows, currentResolutions())).toBe(rows);

    beginLocalElicitationResolution("elicit_a", ["sess_1"], { nativeDelivery: false });
    const adjusted = applyLocalElicitationResolutions(rows, currentResolutions());

    expect(adjusted).not.toBe(rows);
    expect(adjusted[0]).not.toBe(rows[0]);
    expect(adjusted[1]).toBe(rows[1]);
  });

  it("releases a hold at once when the POST fails", () => {
    observeSessionPendingCounts([row("sess_1", 1)]);
    const token = beginLocalElicitationResolution("elicit_a", ["sess_1"], {
      nativeDelivery: false,
    });
    cancelLocalElicitationResolution("elicit_a", token);

    expect(displayedCounts([row("sess_1", 1)])).toEqual({ sess_1: 1 });
    expect(currentResolutions().answeredElicitationIds.size).toBe(0);
  });

  it("releases an acknowledged hold when the session count drops", () => {
    observeSessionPendingCounts([row("sess_1", 1)]);
    const token = beginLocalElicitationResolution("elicit_a", ["sess_1"], {
      nativeDelivery: false,
    });
    acknowledgeLocalElicitationResolution("elicit_a", token);

    expect(displayedCounts([row("sess_1", 1)])).toEqual({ sess_1: 0 });
    observeSessionPendingCounts([row("sess_1", 0)]);

    expect(currentResolutions().answeredElicitationIds.size).toBe(0);
    expect(displayedCounts([row("sess_1", 0)])).toEqual({ sess_1: 0 });
  });

  it("releases a hold whose count drop was observed before the POST returned", () => {
    observeSessionPendingCounts([row("sess_1", 1)]);
    const token = beginLocalElicitationResolution("elicit_a", ["sess_1"], {
      nativeDelivery: false,
    });
    observeSessionPendingCounts([row("sess_1", 0)]);

    expect(currentResolutions().answeredElicitationIds.size).toBe(0);
    // The late acknowledgement finds nothing to settle.
    acknowledgeLocalElicitationResolution("elicit_a", token);
    expect(currentResolutions().answeredElicitationIds.size).toBe(0);
  });

  it.each([
    ["was never loaded", []],
    ["was unloaded before the answer", [[row("sess_1", 2)], []]],
  ] as const)("shows the server count of a session that %s", (_case, observations) => {
    for (const rows of observations) observeSessionPendingCounts(rows);
    beginLocalElicitationResolution("elicit_a", ["sess_1"], { nativeDelivery: false });

    // The server already removed the answered prompt; another one is still pending.
    expect(displayedCounts([row("sess_1", 1)])).toEqual({ sess_1: 1 });
    observeSessionPendingCounts([row("sess_1", 1)]);
    expect(displayedCounts([row("sess_1", 1)])).toEqual({ sess_1: 1 });
  });

  it("releases a hold when its session returns to the rows with a lower count", () => {
    observeSessionPendingCounts([row("sess_1", 2)]);
    beginLocalElicitationResolution("elicit_a", ["sess_1"], { nativeDelivery: false });
    observeSessionPendingCounts([]);

    expect(displayedCounts([row("sess_1", 1)])).toEqual({ sess_1: 1 });
    observeSessionPendingCounts([row("sess_1", 1)]);
    expect(currentResolutions().answeredElicitationIds.size).toBe(0);
  });

  it("shows a new prompt that arrives after the answer's count drop was observed", () => {
    observeSessionPendingCounts([row("sess_1", 1)]);
    const token = beginLocalElicitationResolution("elicit_a", ["sess_1"], {
      nativeDelivery: false,
    });
    acknowledgeLocalElicitationResolution("elicit_a", token);
    observeSessionPendingCounts([row("sess_1", 0)]);
    observeSessionPendingCounts([row("sess_1", 1)]);

    expect(displayedCounts([row("sess_1", 1)])).toEqual({ sess_1: 1 });
  });

  it("shows a new prompt that replaces the answered one within a single tick once the settle window ends", () => {
    observeSessionPendingCounts([row("sess_1", 1)]);
    const token = beginLocalElicitationResolution("elicit_a", ["sess_1"], {
      nativeDelivery: false,
    });
    acknowledgeLocalElicitationResolution("elicit_a", token);
    // The count stays at 1 because the answered prompt was replaced, so no drop shows.
    observeSessionPendingCounts([row("sess_1", 1)]);

    vi.advanceTimersByTime(ACK_SETTLE_MS - 1);
    expect(displayedCounts([row("sess_1", 1)])).toEqual({ sess_1: 0 });

    vi.advanceTimersByTime(1);
    expect(currentResolutions().answeredElicitationIds.size).toBe(0);
    expect(displayedCounts([row("sess_1", 1)])).toEqual({ sess_1: 1 });
  });

  it("keeps counting a prompt that arrives while an answer is held", () => {
    observeSessionPendingCounts([row("sess_1", 1)]);
    beginLocalElicitationResolution("elicit_a", ["sess_1"], { nativeDelivery: false });
    // A second prompt arrives while the first POST is still in flight.
    observeSessionPendingCounts([row("sess_1", 2)]);
    expect(displayedCounts([row("sess_1", 2)])).toEqual({ sess_1: 1 });

    // The server removed the answered prompt before the drop was observed.
    expect(displayedCounts([row("sess_1", 1)])).toEqual({ sess_1: 1 });
    observeSessionPendingCounts([row("sess_1", 1)]);
    expect(currentResolutions().answeredElicitationIds.size).toBe(0);
    expect(displayedCounts([row("sess_1", 1)])).toEqual({ sess_1: 1 });
  });

  it("gives up on a submit whose POST never returns", () => {
    observeSessionPendingCounts([row("sess_1", 1)]);
    beginLocalElicitationResolution("elicit_a", ["sess_1"], { nativeDelivery: false });

    vi.advanceTimersByTime(SUBMIT_TIMEOUT_MS - 1);
    expect(currentResolutions().answeredElicitationIds.has("elicit_a")).toBe(true);
    vi.advanceTimersByTime(1);

    expect(currentResolutions().answeredElicitationIds.size).toBe(0);
    expect(displayedCounts([row("sess_1", 1)])).toEqual({ sess_1: 1 });
  });

  it("holds an acknowledged native answer for the longer native window", () => {
    observeSessionPendingCounts([row("sess_1", 1)]);
    const token = beginLocalElicitationResolution("elicit_a", ["sess_1"], { nativeDelivery: true });
    acknowledgeLocalElicitationResolution("elicit_a", token);

    vi.advanceTimersByTime(ACK_SETTLE_MS + 1);
    expect(displayedCounts([row("sess_1", 1)])).toEqual({ sess_1: 0 });

    vi.advanceTimersByTime(NATIVE_SETTLE_MS - ACK_SETTLE_MS - 2);
    expect(displayedCounts([row("sess_1", 1)])).toEqual({ sess_1: 0 });

    vi.advanceTimersByTime(1);
    expect(displayedCounts([row("sess_1", 1)])).toEqual({ sess_1: 1 });
  });

  it("keeps one hold per elicitation when the same prompt is answered again", () => {
    observeSessionPendingCounts([row("sess_1", 1), row("sess_2", 1)]);
    beginLocalElicitationResolution("elicit_a", ["sess_1"], { nativeDelivery: false });
    beginLocalElicitationResolution("elicit_a", ["sess_2"], { nativeDelivery: false });

    expect(displayedCounts([row("sess_1", 1), row("sess_2", 1)])).toEqual({
      sess_1: 1,
      sess_2: 0,
    });
  });

  it("lets only the latest submission acknowledge or cancel its hold", () => {
    const older = beginLocalElicitationResolution("elicit_a", ["sess_1"], {
      nativeDelivery: false,
    });
    beginLocalElicitationResolution("elicit_a", ["sess_1"], { nativeDelivery: false });

    cancelLocalElicitationResolution("elicit_a", older);
    expect(currentResolutions().answeredElicitationIds.has("elicit_a")).toBe(true);

    // An older acknowledgement must not start the newer POST's settle window.
    acknowledgeLocalElicitationResolution("elicit_a", older);
    vi.advanceTimersByTime(ACK_SETTLE_MS);
    expect(currentResolutions().answeredElicitationIds.has("elicit_a")).toBe(true);
  });

  it("treats a session with no reported count as zero", () => {
    observeSessionPendingCounts([row("sess_1", 1)]);
    const token = beginLocalElicitationResolution("elicit_a", ["sess_1"], {
      nativeDelivery: false,
    });
    acknowledgeLocalElicitationResolution("elicit_a", token);
    observeSessionPendingCounts([row("sess_1", undefined)]);

    expect(currentResolutions().answeredElicitationIds.size).toBe(0);
  });
});

describe("affectedSessionIds", () => {
  it("returns the starting sessions and each ancestor once, nearest first", () => {
    const parents: Record<string, string> = { child: "parent", parent: "root" };
    const parentOf = (sessionId: string) => parents[sessionId];

    expect(affectedSessionIds(["child", "child", undefined], parentOf)).toEqual([
      "child",
      "parent",
      "root",
    ]);
  });

  it("stops on a parent cycle", () => {
    const parents: Record<string, string> = { a: "b", b: "a" };

    expect(affectedSessionIds(["a"], (sessionId) => parents[sessionId])).toEqual(["a", "b"]);
  });

  it("returns only the starting sessions when there is no parent", () => {
    expect(affectedSessionIds(["sess_1", null], NO_PARENT)).toEqual(["sess_1"]);
  });
});
