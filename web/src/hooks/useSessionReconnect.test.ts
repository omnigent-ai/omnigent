import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";

import { controlHost, getHostIdentity, isElectronShell } from "@/lib/nativeBridge";
import type { HostActionResult, HostIdentity } from "@/lib/nativeBridge";
import { useSessionReconnect } from "./useSessionReconnect";

vi.mock("@/lib/nativeBridge", () => ({
  controlHost: vi.fn(),
  getHostIdentity: vi.fn(),
  isElectronShell: vi.fn(),
}));
vi.mock("sonner", () => ({
  toast: { loading: vi.fn(() => "progress"), success: vi.fn(), dismiss: vi.fn() },
}));

const localSession = { sessionId: "session", hostId: "this-mac", isOwner: true };

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(isElectronShell).mockReturnValue(true);
  vi.mocked(getHostIdentity).mockResolvedValue({ cliInstalled: true, hostId: "this-mac" });
  vi.mocked(controlHost).mockResolvedValue({ ok: true });
});
afterEach(cleanup);

describe("useSessionReconnect", () => {
  it("starts the local host on the first action without opening the dialog", async () => {
    const pending = deferred<HostActionResult>();
    vi.mocked(controlHost).mockReturnValue(pending.promise);
    const { result } = renderHook(() => useSessionReconnect(localSession));

    let reconnect!: Promise<void>;
    await act(async () => {
      reconnect = result.current.reconnect();
    });
    expect(controlHost).toHaveBeenCalledExactlyOnceWith("start");
    expect(result.current.dialogOpen).toBe(false);
    expect(result.current.localReconnect?.reconnecting).toBe(true);
    expect(toast.loading).toHaveBeenCalledWith("Reconnecting this machine…");
    expect(toast.success).not.toHaveBeenCalled();

    await act(async () => {
      pending.resolve({ ok: true });
      await reconnect;
    });
    expect(result.current.dialogOpen).toBe(false);
    expect(result.current.localReconnect?.reconnecting).toBe(false);
    expect(toast.success).toHaveBeenCalledWith("Host reconnected.");
    expect(toast.dismiss).toHaveBeenCalledWith("progress");
  });

  it("ignores repeated clicks during both identity lookup and host startup", async () => {
    const identity = deferred<HostIdentity>();
    const start = deferred<HostActionResult>();
    vi.mocked(getHostIdentity).mockReturnValue(identity.promise);
    vi.mocked(controlHost).mockReturnValue(start.promise);
    const { result } = renderHook(() => useSessionReconnect(localSession));
    let first!: Promise<void>;
    await act(async () => {
      first = result.current.reconnect();
      await result.current.reconnect();
    });
    expect(getHostIdentity).toHaveBeenCalledOnce();
    await act(async () => {
      identity.resolve({ cliInstalled: true, hostId: "this-mac" });
    });
    await act(async () => {
      await result.current.reconnect();
    });
    expect(controlHost).toHaveBeenCalledOnce();
    await act(async () => {
      start.resolve({ ok: true });
      await first;
    });
  });

  it.each([
    { label: "another machine", identity: { cliInstalled: true, hostId: "other" } },
    { label: "missing CLI", identity: { cliInstalled: false, hostId: "this-mac" } },
    { label: "missing identity", identity: null },
  ])("opens the fallback for $label without starting a host", async ({ identity }) => {
    vi.mocked(getHostIdentity).mockResolvedValue(identity);
    const { result } = renderHook(() => useSessionReconnect(localSession));
    await act(async () => {
      await result.current.reconnect();
    });
    expect(result.current.dialogOpen).toBe(true);
    expect(result.current.localReconnect).toBeUndefined();
    expect(controlHost).not.toHaveBeenCalled();
  });

  it.each([
    { label: "browser", desktop: false, hostId: "this-mac", isOwner: true },
    { label: "non-owner", desktop: true, hostId: "this-mac", isOwner: false },
    { label: "unbound session", desktop: true, hostId: null, isOwner: true },
  ])("uses the dialog for a $label without querying the desktop", async ({ desktop, ...props }) => {
    vi.mocked(isElectronShell).mockReturnValue(desktop);
    const { result } = renderHook(() => useSessionReconnect({ ...localSession, ...props }));
    await act(async () => {
      await result.current.reconnect();
    });
    expect(result.current.dialogOpen).toBe(true);
    expect(getHostIdentity).not.toHaveBeenCalled();
    expect(controlHost).not.toHaveBeenCalled();
  });

  it.each([
    { failure: { ok: false, authError: true }, message: /finish signing in/ },
    { failure: { ok: false }, message: /Try again or run the command/ },
    { failure: { ok: false, error: "Connection timed out" }, message: /Connection timed out/ },
  ])(
    "opens recovery on failure and lets a retry succeed: $failure",
    async ({ failure, message }) => {
      vi.mocked(controlHost).mockResolvedValueOnce(failure);
      const { result } = renderHook(() => useSessionReconnect(localSession));
      await act(async () => {
        await result.current.reconnect();
      });
      expect(result.current.dialogOpen).toBe(true);
      expect(result.current.localReconnect?.error).toMatch(message);
      expect(toast.success).not.toHaveBeenCalled();
      await act(async () => {
        await result.current.reconnect();
      });
      expect(result.current.dialogOpen).toBe(false);
      expect(result.current.localReconnect?.error).toBeNull();
      expect(controlHost).toHaveBeenCalledTimes(2);
      expect(toast.success).toHaveBeenCalledOnce();
    },
  );

  it("rechecks the machine identity before retrying", async () => {
    vi.mocked(controlHost).mockResolvedValue({ ok: false });
    const { result } = renderHook(() => useSessionReconnect(localSession));
    await act(async () => {
      await result.current.reconnect();
    });
    vi.mocked(getHostIdentity).mockResolvedValue({ cliInstalled: true, hostId: "new-host-id" });
    await act(async () => {
      await result.current.reconnect();
    });
    expect(controlHost).toHaveBeenCalledOnce();
    expect(result.current.localReconnect).toBeUndefined();
    expect(result.current.dialogOpen).toBe(true);
  });

  it("does not start a host after navigating away during identity lookup", async () => {
    const identity = deferred<HostIdentity>();
    vi.mocked(getHostIdentity).mockReturnValue(identity.promise);
    const { result, rerender } = renderHook(useSessionReconnect, { initialProps: localSession });
    let reconnect!: Promise<void>;
    await act(async () => {
      reconnect = result.current.reconnect();
    });
    rerender({ ...localSession, sessionId: "other-session" });
    await act(async () => {
      identity.resolve({ cliInstalled: true, hostId: "this-mac" });
      await reconnect;
    });
    expect(controlHost).not.toHaveBeenCalled();
    expect(result.current.dialogOpen).toBe(false);
  });
});
