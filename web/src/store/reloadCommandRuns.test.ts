import { afterEach, describe, expect, it, vi } from "vitest";
import {
  clearUndeliveredReloadCommandRuns,
  isReloadCommand,
  noteReloadCommandRun,
  subscribeReloadCommandRuns,
} from "./reloadCommandRuns";

afterEach(() => clearUndeliveredReloadCommandRuns());

describe("reloadCommandRuns", () => {
  it("recognizes only the surfaced reload commands", () => {
    expect(isReloadCommand("command", "reload-skills")).toBe(true);
    expect(isReloadCommand("command", "reload-plugins")).toBe(true);
    expect(isReloadCommand("skill", "reload-skills")).toBe(false);
    expect(isReloadCommand("command", "compact")).toBe(false);
  });

  it("delivers each run to the conversation's listeners", () => {
    const listener = vi.fn();
    const other = vi.fn();
    const unsubscribe = subscribeReloadCommandRuns("conv_a", listener);
    subscribeReloadCommandRuns("conv_b", other)();
    noteReloadCommandRun("conv_a");
    noteReloadCommandRun("conv_a");
    expect(listener).toHaveBeenCalledTimes(2);
    expect(other).not.toHaveBeenCalled();
    unsubscribe();
    noteReloadCommandRun("conv_a");
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it("holds runs nobody heard until the next subscriber, once", () => {
    noteReloadCommandRun("conv_a");
    noteReloadCommandRun("conv_a");
    const first = vi.fn();
    subscribeReloadCommandRuns("conv_a", first)();
    expect(first).toHaveBeenCalledOnce();
    const second = vi.fn();
    subscribeReloadCommandRuns("conv_a", second)();
    expect(second).not.toHaveBeenCalled();
  });

  it("keeps a resubscribed listener when an old unsubscribe runs again", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeReloadCommandRuns("conv_a", vi.fn());
    unsubscribe();
    const unsubscribeListener = subscribeReloadCommandRuns("conv_a", listener);
    unsubscribe();
    noteReloadCommandRun("conv_a");
    expect(listener).toHaveBeenCalledOnce();
    unsubscribeListener();
  });
});
