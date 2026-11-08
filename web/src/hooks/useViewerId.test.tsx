import { act, renderHook } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

const identity = vi.hoisted(() => ({
  resolution: Promise.resolve<string | null>(null),
  viewerId: null as string | null,
  listeners: new Set<() => void>(),
}));

vi.mock("@/lib/identity", () => ({
  getCurrentUserId: () => identity.viewerId,
  resolveIdentity: () => identity.resolution,
  subscribeIdentity: (listener: () => void) => {
    identity.listeners.add(listener);
    return () => identity.listeners.delete(listener);
  },
}));

import { useIdentityReady, useViewerId } from "./useViewerId";

beforeEach(() => {
  identity.resolution = Promise.resolve(null);
  identity.viewerId = null;
  identity.listeners.clear();
});

it("waits until identity resolves", async () => {
  let resolve!: (value: string | null) => void;
  identity.resolution = new Promise((finish) => {
    resolve = finish;
  });
  const { result } = renderHook(useIdentityReady);

  expect(result.current).toBe(false);
  await act(async () => resolve("alice"));
  expect(result.current).toBe(true);
});

it("unblocks when identity resolution fails", async () => {
  let reject!: (error: Error) => void;
  identity.resolution = new Promise((_resolve, fail) => {
    reject = fail;
  });
  const { result } = renderHook(useIdentityReady);

  expect(result.current).toBe(false);
  await act(async () => reject(new Error("offline")));
  expect(result.current).toBe(true);
});

it("shows the viewer id once a later probe recovers it", async () => {
  const { result, unmount } = renderHook(useViewerId);
  await act(async () => {});
  expect(result.current).toBeNull();

  identity.viewerId = "alice";
  act(() => {
    for (const listener of identity.listeners) listener();
  });
  expect(result.current).toBe("alice");

  unmount();
  expect(identity.listeners.size).toBe(0);
});
