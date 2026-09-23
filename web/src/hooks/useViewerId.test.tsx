import { act, renderHook } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

const identity = vi.hoisted(() => ({
  resolution: Promise.resolve<string | null>(null),
}));

vi.mock("@/lib/identity", () => ({
  getCurrentUserId: () => null,
  resolveIdentity: () => identity.resolution,
}));

import { useIdentityReady } from "./useViewerId";

beforeEach(() => {
  identity.resolution = Promise.resolve(null);
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
