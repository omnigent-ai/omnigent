import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import { createHeldPolls, readOnceOrReschedule } from "./heldPolls";

const poll = () => ({ observers: [{ options: { queryKey: ["q"] }, setOptions: vi.fn() }] });

describe("createHeldPolls", () => {
  it("lets a released poll go, and counts it once when it's held again", () => {
    const held = createHeldPolls();
    const first = poll();
    held.hold(first);
    held.release(first);
    expect(held.size()).toBe(0);
    held.hold(first);
    held.hold(first);
    expect(held.size()).toBe(1);
    held.reschedule();
    expect(first.observers[0]!.setOptions).toHaveBeenCalledTimes(1);
  });

  it("lets go of a poll whose observers all left after it was held", () => {
    const client = new QueryClient();
    const observer = new QueryObserver(client, { queryKey: ["q"], queryFn: () => "list" });
    const stop = observer.subscribe(() => undefined);
    const query = client.getQueryCache().find({ queryKey: ["q"], exact: true })!;
    const held = createHeldPolls();
    held.hold(query);
    expect(held.size()).toBe(1);
    stop();
    expect(held.size()).toBe(0);
    expect(held.drain()).toEqual([]);
    client.clear();
  });

  it("reads each poll allowed to read once, and reschedules the rest", () => {
    const polling = (refetchInterval: number | false) => {
      const observer = {
        options: { queryKey: ["q"], refetchInterval },
        setOptions: vi.fn(),
        refetch: vi.fn(async () => undefined),
      };
      return { poll: { observers: [observer] }, observer };
    };
    const allowed = polling(30_000);
    const held = polling(30_000);
    const off = polling(false);
    readOnceOrReschedule(
      [allowed.poll, held.poll, off.poll],
      (candidate) => candidate !== held.poll,
    );
    expect(allowed.observer.refetch).toHaveBeenCalledWith({ cancelRefetch: false });
    expect(allowed.observer.setOptions).not.toHaveBeenCalled();
    for (const { observer } of [held, off]) {
      expect(observer.refetch).not.toHaveBeenCalled();
      expect(observer.setOptions).toHaveBeenCalledTimes(1);
    }
  });
});
