import { describe, expect, it, vi } from "vitest";
import {
  getSessionSummaryRevision,
  notifySessionSummariesMayHaveChanged,
  subscribeSessionSummaryChanges,
} from "./sessionSummaryChanges";

describe("sessionSummaryChanges", () => {
  it("increments a monotonic revision and notifies subscribers", () => {
    const listener = vi.fn();
    const before = getSessionSummaryRevision();
    const unsubscribe = subscribeSessionSummaryChanges(listener);

    notifySessionSummariesMayHaveChanged();

    expect(getSessionSummaryRevision()).toBe(before + 1);
    expect(listener).toHaveBeenCalledOnce();
    unsubscribe();
    notifySessionSummariesMayHaveChanged();
    expect(listener).toHaveBeenCalledOnce();
  });
});
