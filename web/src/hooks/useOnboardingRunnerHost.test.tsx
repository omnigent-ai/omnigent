import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Host } from "@/hooks/useHosts";
import { ONBOARDING_RUNNER_GRACE_MS, useOnboardingRunnerHost } from "./useOnboardingRunnerHost";

const bridge = vi.hoisted(() => ({
  takeOnboardingRunner: vi.fn<() => Promise<"local" | "remote" | null>>(),
  getHostIdentity: vi.fn<() => Promise<{ cliInstalled: boolean; hostId: string | null } | null>>(),
}));
vi.mock("@/lib/nativeBridge", () => ({
  isElectronShell: () => true,
  takeOnboardingRunner: bridge.takeOnboardingRunner,
  getHostIdentity: bridge.getHostIdentity,
}));

beforeEach(() => {
  bridge.getHostIdentity.mockResolvedValue({ cliInstalled: true, hostId: "laptop" });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  bridge.takeOnboardingRunner.mockReset();
  bridge.getHostIdentity.mockReset();
});

function host(host_id: string, status: Host["status"] = "online"): Host {
  return { host_id, name: host_id, owner: "me", status };
}

describe("useOnboardingRunnerHost", () => {
  it("holds the default until the shell answers, then lets it through when nothing was picked", async () => {
    bridge.takeOnboardingRunner.mockResolvedValue(null);
    const { result } = renderHook(() => useOnboardingRunnerHost([host("laptop")]));
    expect(result.current.pending).toBe(true);
    await waitFor(() => expect(result.current.pending).toBe(false));
    expect(result.current.hostId).toBeNull();
    expect(bridge.takeOnboardingRunner).toHaveBeenCalledOnce();
  });

  it("resolves 'local' to this machine's host once it's online", async () => {
    bridge.takeOnboardingRunner.mockResolvedValue("local");
    const { result, rerender } = renderHook(({ hosts }) => useOnboardingRunnerHost(hosts), {
      initialProps: { hosts: [host("laptop", "offline"), host("box")] },
    });
    await waitFor(() => expect(bridge.takeOnboardingRunner).toHaveBeenCalled());
    expect(result.current).toMatchObject({ pending: true, hostId: null });
    rerender({ hosts: [host("laptop"), host("box")] });
    expect(result.current).toEqual({ pending: false, hostId: "laptop" });
  });

  it("resolves 'remote' to the only other online host, and gives up with several", async () => {
    bridge.takeOnboardingRunner.mockResolvedValue("remote");
    const { result, rerender } = renderHook(({ hosts }) => useOnboardingRunnerHost(hosts), {
      initialProps: { hosts: [host("laptop")] },
    });
    await waitFor(() => expect(bridge.takeOnboardingRunner).toHaveBeenCalled());
    await act(async () => {});
    // Still booting: nothing but this machine is online yet.
    expect(result.current.pending).toBe(true);

    rerender({ hosts: [host("laptop"), host("box")] });
    expect(result.current).toEqual({ pending: false, hostId: "box" });

    rerender({ hosts: [host("laptop"), host("box"), host("other")] });
    expect(result.current).toEqual({ pending: false, hostId: null });
  });

  it("stops waiting after the grace period", async () => {
    vi.useFakeTimers();
    bridge.takeOnboardingRunner.mockResolvedValue("remote");
    const { result } = renderHook(() => useOnboardingRunnerHost([]));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.pending).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ONBOARDING_RUNNER_GRACE_MS);
    });
    expect(result.current.pending).toBe(false);
  });

  it("keeps a resolved runner past the grace period", async () => {
    vi.useFakeTimers();
    bridge.takeOnboardingRunner.mockResolvedValue("local");
    const { result } = renderHook(() => useOnboardingRunnerHost([host("laptop")]));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.hostId).toBe("laptop");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ONBOARDING_RUNNER_GRACE_MS * 2);
    });
    expect(result.current).toEqual({ pending: false, hostId: "laptop" });
  });

  it("treats a failed handoff as nothing picked", async () => {
    bridge.takeOnboardingRunner.mockRejectedValue(new Error("ipc down"));
    const { result } = renderHook(() => useOnboardingRunnerHost([host("laptop")]));
    await waitFor(() => expect(result.current.pending).toBe(false));
    expect(result.current.hostId).toBeNull();
  });

  it("gives up without this machine's identity, rather than guess", async () => {
    bridge.getHostIdentity.mockResolvedValue(null);
    bridge.takeOnboardingRunner.mockResolvedValue("remote");
    // The only online host could be this laptop.
    const { result } = renderHook(() => useOnboardingRunnerHost([host("laptop")]));
    await waitFor(() => expect(result.current.pending).toBe(false));
    expect(result.current.hostId).toBeNull();
  });

  it("resolves 'remote' on a laptop that never hosted", async () => {
    bridge.getHostIdentity.mockResolvedValue({ cliInstalled: false, hostId: null });
    bridge.takeOnboardingRunner.mockResolvedValue("remote");
    const { result } = renderHook(() => useOnboardingRunnerHost([host("box")]));
    await waitFor(() => expect(result.current).toEqual({ pending: false, hostId: "box" }));
  });

  it("gives up on 'local' right away when this machine has no host id", async () => {
    bridge.getHostIdentity.mockResolvedValue({ cliInstalled: true, hostId: null });
    bridge.takeOnboardingRunner.mockResolvedValue("local");
    const { result } = renderHook(() => useOnboardingRunnerHost([host("box")]));
    await waitFor(() => expect(result.current.pending).toBe(false));
    expect(result.current.hostId).toBeNull();
  });

  it("stops waiting even if the shell never answers", async () => {
    vi.useFakeTimers();
    bridge.takeOnboardingRunner.mockReturnValue(new Promise(() => {}));
    const { result } = renderHook(() => useOnboardingRunnerHost([host("laptop")]));
    expect(result.current.pending).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ONBOARDING_RUNNER_GRACE_MS);
    });
    expect(result.current.pending).toBe(false);
  });

  it("doesn't take this laptop for the remote host before its identity loads", async () => {
    let resolveIdentity: (v: { cliInstalled: boolean; hostId: string }) => void = () => {};
    bridge.getHostIdentity.mockReturnValue(
      new Promise((resolve) => {
        resolveIdentity = resolve;
      }),
    );
    bridge.takeOnboardingRunner.mockResolvedValue("remote");
    const { result, rerender } = renderHook(({ hosts }) => useOnboardingRunnerHost(hosts), {
      initialProps: { hosts: [host("laptop")] },
    });
    await waitFor(() => expect(bridge.getHostIdentity).toHaveBeenCalled());
    // Only the laptop is online and its identity is unknown: keep waiting.
    expect(result.current).toEqual({ pending: true, hostId: null });
    await act(async () => resolveIdentity({ cliInstalled: true, hostId: "laptop" }));
    expect(result.current).toEqual({ pending: true, hostId: null });
    rerender({ hosts: [host("laptop"), host("box")] });
    expect(result.current).toEqual({ pending: false, hostId: "box" });
  });
});
