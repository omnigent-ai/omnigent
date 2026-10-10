import { act, renderHook } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

const identity = vi.hoisted(() => ({
  resolution: Promise.resolve<string | null>(null),
  userId: null as string | null,
  listeners: new Set<() => void>(),
}));

vi.mock("@/lib/identity", () => ({
  getCurrentUserId: () => identity.userId,
  resolveIdentity: () => identity.resolution,
  subscribeIdentity: (listener: () => void) => {
    identity.listeners.add(listener);
    return () => {
      identity.listeners.delete(listener);
    };
  },
}));

import { useIdentityReady, useViewerId } from "./useViewerId";

beforeEach(() => {
  identity.resolution = Promise.resolve(null);
  identity.userId = null;
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

it("reports the viewer once the probe resolves", async () => {
  let resolve!: (value: string | null) => void;
  identity.resolution = new Promise((finish) => {
    resolve = finish;
  });
  const { result } = renderHook(useViewerId);

  expect(result.current).toBeNull();
  identity.userId = "alice";
  await act(async () => resolve("alice"));
  expect(result.current).toBe("alice");
});

it("follows an identity that settles after the boot probe failed", async () => {
  // The boot probe resolved null (transient failure); a later successful probe
  // publishes the viewer through the subscription rather than a new mount.
  const { result } = renderHook(useViewerId);
  await act(async () => {
    await identity.resolution;
  });
  expect(result.current).toBeNull();

  identity.userId = "alice";
  act(() => {
    for (const listener of identity.listeners) listener();
  });
  expect(result.current).toBe("alice");
});

it("stops listening on unmount", () => {
  const { unmount } = renderHook(useViewerId);
  expect(identity.listeners.size).toBe(1);

  unmount();
  expect(identity.listeners.size).toBe(0);
});
