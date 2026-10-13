import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { removeStaticSandbox, wakeSessionHost } from "@/lib/staticSandboxesApi";
import type { SessionLiveness } from "./useSessionLiveness";
import {
  StaticSandboxWakeContext,
  useStaticSandboxWake,
  useStaticSandboxWakeOnInput,
  type StaticSandboxWake,
} from "./useStaticSandboxWake";

vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));
vi.mock("@/lib/staticSandboxesApi", () => ({
  removeStaticSandbox: vi.fn(() => Promise.resolve()),
  wakeSessionHost: vi.fn(() => Promise.resolve()),
}));

const asleep = { kind: "host_asleep" } as SessionLiveness;
const online = { kind: "online" } as SessionLiveness;
const paused = {
  hostResumable: true,
  sandboxState: "paused",
  sandboxName: "foo",
  sandboxNameProvider: "remote",
} as const;

afterEach(() => vi.clearAllMocks());

describe("useStaticSandboxWake", () => {
  it("offers Resume while the host sleeps and marks the wake in flight", async () => {
    let resolveWake = () => {};
    vi.mocked(wakeSessionHost).mockReturnValue(
      new Promise<void>((r) => {
        resolveWake = r;
      }),
    );
    const { result } = renderHook(() => useStaticSandboxWake("s1", asleep, paused));
    expect(result.current.pausedWakeKey).toBe("s1");
    expect(result.current.pausedSandbox).toMatchObject({ name: "foo", state: "paused" });
    act(() => result.current.pausedSandbox!.onResume());
    expect(wakeSessionHost).toHaveBeenCalledWith("s1");
    expect(result.current.pausedSandbox?.waking).toBe(true);
    resolveWake();
    await waitFor(() => expect(result.current.pausedSandbox?.waking).toBe(false));
  });

  it("shows a gone VM even while online, but never wakes it", () => {
    const { result } = renderHook(() =>
      useStaticSandboxWake("s1", online, { ...paused, sandboxState: "gone" }),
    );
    expect(result.current.pausedWakeKey).toBeNull();
    result.current.pausedSandbox!.onRemove();
    expect(removeStaticSandbox).toHaveBeenCalledWith("remote", "foo");
  });

  it("is empty for an online host or a host without in-place resume", () => {
    const up = renderHook(() => useStaticSandboxWake("s1", online, paused)).result.current;
    expect(up.pausedSandbox).toBeNull();
    expect(up.pausedWakeKey).toBeNull();
    const managed = renderHook(() =>
      useStaticSandboxWake("s1", asleep, { ...paused, hostResumable: false }),
    ).result.current;
    expect(managed.pausedWakeKey).toBeNull();
  });
});

describe("useStaticSandboxWakeOnInput", () => {
  function provider(value: StaticSandboxWake | null) {
    return ({ children }: { children: ReactNode }) => (
      <StaticSandboxWakeContext.Provider value={value}>
        {children}
      </StaticSandboxWakeContext.Provider>
    );
  }

  it("wakes the provided session's host on the first keystroke", () => {
    const wake = vi.fn();
    const ctx = { pausedSandbox: null, pausedWakeKey: "s1", wake };
    const { rerender } = renderHook(({ text }) => useStaticSandboxWakeOnInput(text), {
      initialProps: { text: "" },
      wrapper: provider(ctx),
    });
    expect(wake).not.toHaveBeenCalled();
    rerender({ text: "h" });
    rerender({ text: "hi" });
    expect(wake).toHaveBeenCalledOnce();
  });

  it("does nothing outside a chat session", () => {
    expect(() => renderHook(() => useStaticSandboxWakeOnInput("hi"))).not.toThrow();
  });
});
