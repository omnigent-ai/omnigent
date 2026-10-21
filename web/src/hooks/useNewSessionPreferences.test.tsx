import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useNewSessionPreferences } from "./useNewSessionPreferences";
import {
  hostPreferenceAction,
  readSharedPreferences,
  type HostPreferences,
} from "@/lib/sharedPreferences";

const scope = vi.hoisted(() => ({ user: "alice", server: "workspace", enabled: true }));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ shared_preferences: scope.enabled }),
}));
vi.mock("@/lib/host", () => ({ getOmnigentServerIdentity: () => scope.server }));
vi.mock("@/lib/identity", () => ({
  getCurrentUserId: () => scope.user,
  subscribeIdentity: () => () => {},
}));
vi.mock("@/lib/sharedPreferences", () => ({
  hostPreferenceAction: vi.fn(),
  readSharedPreferences: vi.fn(),
}));
const prepared: HostPreferences = {
  connected: true,
  machine: [],
  pending: [],
  preview: null,
  warning: null,
  shared: null,
  launch: {
    protocol_version: 1,
    installation_id: "machine",
    state: "current",
    warning: null,
    machine: [],
    shared: {
      protocol_version: 2,
      identity: { authority_id: "server", user_id: "alice", workspace_id: "workspace" },
      revision: 1,
      entries: [],
      conflicts: [],
    },
  },
  defaults: { model: "chosen" },
};
beforeEach(() => {
  vi.useFakeTimers();
  vi.resetAllMocks();
  scope.user = "alice";
  scope.enabled = true;
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
const advance = (ms: number) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

it("waits for a target, debounces workspace input, and does not poll after success", async () => {
  vi.mocked(hostPreferenceAction).mockResolvedValue(prepared);
  const { result, rerender } = renderHook(
    ({ host, workspace }) => useNewSessionPreferences(host, false, workspace),
    { initialProps: { host: null as string | null, workspace: "" } },
  );
  await advance(5_000);
  expect(hostPreferenceAction).not.toHaveBeenCalled();
  rerender({ host: "host", workspace: "/one" });
  await advance(100);
  rerender({ host: "host", workspace: "/two" });
  await advance(250);
  expect(hostPreferenceAction).toHaveBeenCalledTimes(1);
  expect(hostPreferenceAction).toHaveBeenCalledWith(
    "host",
    { action: "prepare", workspace: "/two" },
    expect.any(AbortSignal),
  );
  expect(result.current.launch).toEqual(prepared.launch);
  await advance(60_000);
  window.dispatchEvent(new Event("focus"));
  expect(hostPreferenceAction).toHaveBeenCalledTimes(1);
});

it("ignores late answers after timeout or an explicit temporary choice", async () => {
  let finish!: (value: HostPreferences) => void;
  vi.mocked(hostPreferenceAction).mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const { result } = renderHook(() => useNewSessionPreferences("host", false, "/work"));
  await advance(5_000);
  expect(result.current.error).toContain("in time");
  expect(result.current.blocked).toBe(true);
  act(() => result.current.useTemporary());
  await act(async () => finish(prepared));
  expect(result.current.temporary).toBe(true);
  expect(result.current.launch).toBeUndefined();
  expect(result.current.blocked).toBe(false);
  vi.mocked(hostPreferenceAction).mockResolvedValue(prepared);
  act(() => result.current.retry());
  await advance(250);
  expect(result.current.launch).toEqual(prepared.launch);
});

it("never carries a pending answer or temporary opt-out into another account", async () => {
  let finish!: (value: HostPreferences) => void;
  vi.mocked(hostPreferenceAction).mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const { result, rerender } = renderHook(() => useNewSessionPreferences("host", false, "/work"));
  await advance(250);
  scope.user = "bob";
  rerender();
  await act(async () => finish(prepared));
  expect(result.current.launch).toBeUndefined();
  expect(result.current.blocked).toBe(true);
  act(() => result.current.useTemporary());
  scope.user = "carol";
  rerender();
  expect(result.current.temporary).toBe(false);
});

it("loads managed defaults once without asking any host for credentials", async () => {
  vi.mocked(readSharedPreferences).mockResolvedValue({
    ...prepared.launch!.shared,
    entries: [{ key: { kind: "harness", name: "default" }, version: 1, value: "pi-native" }],
  });
  const { result } = renderHook(() => useNewSessionPreferences(null, true, ""));
  await advance(250);
  expect(result.current.defaults?.default_harness).toBe("pi-native");
  expect(hostPreferenceAction).not.toHaveBeenCalled();
  expect(result.current.launch?.machine).toEqual([]);
});
