import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";

import { fetchHosts } from "@/hooks/useHosts";
import type { Host } from "@/hooks/useHosts";
import { readArcaHostId, writeArcaHostId } from "@/lib/arcaHost";
import { connectArcaHost, controlHost, getHostIdentity, isElectronShell } from "@/lib/nativeBridge";
import type { ArcaConnectResult, HostActionResult, HostIdentity } from "@/lib/nativeBridge";
import { useSessionReconnect } from "./useSessionReconnect";

vi.mock("@/hooks/useHosts", () => ({ fetchHosts: vi.fn() }));
vi.mock("@/lib/nativeBridge", () => ({
  connectArcaHost: vi.fn(),
  controlHost: vi.fn(),
  getHostIdentity: vi.fn(),
  isElectronShell: vi.fn(),
}));
vi.mock("sonner", () => ({
  toast: { loading: vi.fn(() => "progress"), success: vi.fn(), dismiss: vi.fn() },
}));

const localSession = { sessionId: "session", hostId: "this-mac", isOwner: true };

function hostRow(hostId: string, status: Host["status"]): Host {
  return { host_id: hostId, name: hostId, owner: "e2e", status };
}

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
  vi.mocked(connectArcaHost).mockResolvedValue({ ok: true });
  vi.mocked(fetchHosts).mockResolvedValue([hostRow("arca-host", "online")]);
  writeArcaHostId(null);
});
afterEach(() => {
  cleanup();
  writeArcaHostId(null);
});

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
    expect(toast.success).toHaveBeenCalledWith("This machine is back online.");
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

  it("starts the remembered Arca host on the first action without querying this machine", async () => {
    writeArcaHostId("arca-host");
    const pending = deferred<ArcaConnectResult>();
    vi.mocked(connectArcaHost).mockReturnValue(pending.promise);
    const { result } = renderHook(() =>
      useSessionReconnect({ ...localSession, hostId: "arca-host" }),
    );

    let reconnect!: Promise<void>;
    await act(async () => {
      reconnect = result.current.reconnect();
    });
    expect(connectArcaHost).toHaveBeenCalledOnce();
    expect(result.current.dialogOpen).toBe(false);
    expect(result.current.arcaReconnect?.reconnecting).toBe(true);
    expect(toast.loading).toHaveBeenCalledWith("Reconnecting Arca…");
    expect(toast.success).not.toHaveBeenCalled();
    expect(getHostIdentity).not.toHaveBeenCalled();
    expect(controlHost).not.toHaveBeenCalled();

    await act(async () => {
      pending.resolve({ ok: true });
      await reconnect;
    });
    expect(result.current.dialogOpen).toBe(false);
    expect(result.current.arcaReconnect?.reconnecting).toBe(false);
    expect(toast.success).toHaveBeenCalledWith("Arca is back online.");
    expect(toast.dismiss).toHaveBeenCalledWith("progress");
  });

  it("reports an Arca host that was already running as already online", async () => {
    writeArcaHostId("arca-host");
    vi.mocked(connectArcaHost).mockResolvedValue({ ok: true, alreadyRunning: true });
    const { result } = renderHook(() =>
      useSessionReconnect({ ...localSession, hostId: "arca-host" }),
    );

    await act(async () => {
      await result.current.reconnect();
    });

    expect(result.current.dialogOpen).toBe(false);
    expect(toast.success).toHaveBeenCalledExactlyOnceWith("Arca is already online.");
  });

  it("ignores repeated reconnect actions while Arca starts", async () => {
    writeArcaHostId("arca-host");
    const pending = deferred<ArcaConnectResult>();
    vi.mocked(connectArcaHost).mockReturnValue(pending.promise);
    const { result } = renderHook(() =>
      useSessionReconnect({ ...localSession, hostId: "arca-host" }),
    );
    let first!: Promise<void>;
    await act(async () => {
      first = result.current.reconnect();
    });

    await act(async () => {
      await result.current.reconnect();
      result.current.arcaReconnect?.onReconnect();
    });
    expect(connectArcaHost).toHaveBeenCalledOnce();

    await act(async () => {
      pending.resolve({ ok: true });
      await first;
    });
    expect(toast.success).toHaveBeenCalledOnce();
  });

  it.each([null, "previous-arca-host"])(
    "starts a CLI-seeded Arca host when the remembered host is %s",
    async (rememberedHostId) => {
      writeArcaHostId(rememberedHostId);
      const { result } = renderHook(() =>
        useSessionReconnect({ ...localSession, hostId: "arca-host", hostName: "e2e's arca" }),
      );

      await act(async () => {
        await result.current.reconnect();
      });

      expect(connectArcaHost).toHaveBeenCalledOnce();
      expect(result.current.dialogOpen).toBe(false);
      expect(getHostIdentity).not.toHaveBeenCalled();
      expect(controlHost).not.toHaveBeenCalled();
      expect(toast.success).toHaveBeenCalledWith("Arca is back online.");
    },
  );

  it("reports success and remembers the host when Arca comes back as the session's host", async () => {
    writeArcaHostId(null);
    vi.mocked(connectArcaHost).mockResolvedValue({
      ok: true,
      identity: { serverUrl: "https://example.test", hostId: "arca-host" },
    });
    const { result } = renderHook(() =>
      useSessionReconnect({ ...localSession, hostId: "arca-host", hostName: "e2e's arca" }),
    );

    await act(async () => {
      await result.current.reconnect();
    });

    expect(result.current.dialogOpen).toBe(false);
    expect(toast.success).toHaveBeenCalledExactlyOnceWith("Arca is back online.");
    expect(readArcaHostId()).toBe("arca-host");
  });

  it.each([
    { label: "started, identity read", alreadyRunning: false, identityHostId: "current-arca-host" },
    { label: "running, identity read", alreadyRunning: true, identityHostId: "current-arca-host" },
    { label: "running, identity unreadable", alreadyRunning: true, identityHostId: null },
  ])(
    "keeps the dialog open when Arca comes back as a different host ($label)",
    async ({ alreadyRunning, identityHostId }) => {
      writeArcaHostId("previous-arca-host");
      vi.mocked(connectArcaHost).mockResolvedValue({
        ok: true,
        alreadyRunning,
        ...(identityHostId === null
          ? {}
          : { identity: { serverUrl: "https://example.test", hostId: identityHostId } }),
      });
      vi.mocked(fetchHosts).mockResolvedValue([
        hostRow("arca-host", "offline"),
        hostRow("current-arca-host", "online"),
      ]);
      const { result } = renderHook(() =>
        useSessionReconnect({ ...localSession, hostId: "arca-host", hostName: "e2e's arca" }),
      );

      await act(async () => {
        await result.current.reconnect();
      });

      expect(connectArcaHost).toHaveBeenCalledOnce();
      expect(result.current.dialogOpen).toBe(true);
      expect(result.current.arcaReconnect?.error).toMatch(/different host.*Switch host/);
      expect(result.current.arcaReconnect?.reconnecting).toBe(false);
      expect(toast.success).not.toHaveBeenCalled();
      expect(toast.dismiss).toHaveBeenCalledWith("progress");
      expect(readArcaHostId()).toBe(identityHostId ?? "previous-arca-host");
    },
  );

  it("keeps the dialog open when it can't check the session's host", async () => {
    writeArcaHostId("arca-host");
    vi.mocked(fetchHosts).mockRejectedValue(new Error("503 Service Unavailable"));
    const { result } = renderHook(() =>
      useSessionReconnect({ ...localSession, hostId: "arca-host" }),
    );

    await act(async () => {
      await result.current.reconnect();
    });

    expect(result.current.dialogOpen).toBe(true);
    expect(result.current.arcaReconnect?.error).toBe(
      "Couldn't check whether this session's host is back online. Try again.",
    );
    expect(result.current.arcaReconnect?.reconnecting).toBe(false);
    expect(toast.success).not.toHaveBeenCalled();
    expect(toast.dismiss).toHaveBeenCalledWith("progress");
  });

  it("does not treat another remote host as Arca", async () => {
    const { result } = renderHook(() =>
      useSessionReconnect({ ...localSession, hostId: "devbox", hostName: "arca-devbox" }),
    );

    await act(async () => {
      await result.current.reconnect();
    });

    expect(connectArcaHost).not.toHaveBeenCalled();
    expect(controlHost).not.toHaveBeenCalled();
    expect(result.current.dialogOpen).toBe(true);
    expect(result.current.arcaReconnect).toBeUndefined();
  });

  it.each([
    { label: "browser", desktop: false, isOwner: true },
    { label: "non-owner", desktop: true, isOwner: false },
  ])("leaves a $label on the dialog without starting Arca", async ({ desktop, isOwner }) => {
    vi.mocked(isElectronShell).mockReturnValue(desktop);
    const { result } = renderHook(() =>
      useSessionReconnect({
        ...localSession,
        hostId: "arca-host",
        hostName: "e2e's arca",
        isOwner,
      }),
    );

    await act(async () => {
      await result.current.reconnect();
    });

    expect(connectArcaHost).not.toHaveBeenCalled();
    expect(result.current.dialogOpen).toBe(true);
    expect(result.current.arcaReconnect).toBeUndefined();
  });

  it.each([
    { result: { ok: false, canceled: true }, expectedError: null },
    { result: { ok: false, shownInConsole: true }, expectedError: null },
    { result: { ok: false, error: "Arca failed" }, expectedError: "Arca failed" },
  ])("opens the reconnect dialog after an Arca non-success: $result", async (testCase) => {
    writeArcaHostId("arca-host");
    vi.mocked(connectArcaHost).mockResolvedValue(testCase.result);
    const { result } = renderHook(() =>
      useSessionReconnect({ ...localSession, hostId: "arca-host" }),
    );
    await act(async () => {
      await result.current.reconnect();
    });

    expect(connectArcaHost).toHaveBeenCalledOnce();
    expect(result.current.dialogOpen).toBe(true);
    expect(result.current.arcaReconnect?.reconnecting).toBe(false);
    expect(result.current.arcaReconnect?.error).toBe(testCase.expectedError);
    expect(toast.dismiss).toHaveBeenCalledWith("progress");
  });

  it("lets the dialog retry a failed Arca reconnect", async () => {
    writeArcaHostId("arca-host");
    vi.mocked(connectArcaHost).mockResolvedValueOnce({ ok: false, error: "Arca failed" });
    const { result } = renderHook(() =>
      useSessionReconnect({ ...localSession, hostId: "arca-host" }),
    );
    await act(async () => {
      await result.current.reconnect();
    });
    expect(result.current.dialogOpen).toBe(true);
    expect(result.current.arcaReconnect?.error).toBe("Arca failed");

    await act(async () => {
      result.current.arcaReconnect?.onReconnect();
    });
    expect(connectArcaHost).toHaveBeenCalledTimes(2);
    expect(result.current.dialogOpen).toBe(false);
    expect(result.current.arcaReconnect?.error).toBeNull();
    expect(toast.success).toHaveBeenCalledOnce();
  });

  it("clears Arca progress and ignores its result after navigating to another session", async () => {
    writeArcaHostId("arca-host");
    const pending = deferred<ArcaConnectResult>();
    vi.mocked(connectArcaHost).mockReturnValue(pending.promise);
    const { result, rerender } = renderHook(useSessionReconnect, {
      initialProps: { ...localSession, hostId: "arca-host" },
    });
    let reconnect!: Promise<void>;
    await act(async () => {
      reconnect = result.current.reconnect();
    });
    rerender({ ...localSession, sessionId: "other-session", hostId: "other-host" });
    expect(toast.dismiss).toHaveBeenCalledWith("progress");

    await act(async () => {
      pending.resolve({ ok: false, error: "Old failure" });
      await reconnect;
    });
    expect(result.current.dialogOpen).toBe(false);
    expect(result.current.arcaReconnect).toBeUndefined();
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("ignores the host check after navigating to another session", async () => {
    writeArcaHostId("arca-host");
    const hosts = deferred<Host[]>();
    vi.mocked(fetchHosts).mockReturnValue(hosts.promise);
    const { result, rerender } = renderHook(useSessionReconnect, {
      initialProps: { ...localSession, hostId: "arca-host" },
    });
    let reconnect!: Promise<void>;
    await act(async () => {
      reconnect = result.current.reconnect();
    });
    expect(fetchHosts).toHaveBeenCalledOnce();
    rerender({ ...localSession, sessionId: "other-session", hostId: "other-host" });

    await act(async () => {
      hosts.resolve([hostRow("arca-host", "offline")]);
      await reconnect;
    });
    expect(result.current.dialogOpen).toBe(false);
    expect(result.current.arcaReconnect).toBeUndefined();
    expect(toast.success).not.toHaveBeenCalled();
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
    { failure: { ok: false, authError: true }, message: /finish signing in/i },
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

  it("opens another host's recovery while the previous session is still reconnecting", async () => {
    const start = deferred<HostActionResult>();
    vi.mocked(controlHost).mockReturnValue(start.promise);
    const { result, rerender } = renderHook(useSessionReconnect, { initialProps: localSession });
    let first!: Promise<void>;
    await act(async () => {
      first = result.current.reconnect();
    });

    rerender({ ...localSession, sessionId: "other-session", hostId: "other-host" });
    expect(toast.dismiss).toHaveBeenCalledWith("progress");
    await act(async () => {
      await result.current.reconnect();
    });
    expect(result.current.dialogOpen).toBe(true);
    expect(result.current.localReconnect).toBeUndefined();
    expect(controlHost).toHaveBeenCalledOnce();

    await act(async () => {
      start.resolve({ ok: true });
      await first;
    });
    expect(result.current.dialogOpen).toBe(true);
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("does not let an old reconnect clear a newer session's progress or lock", async () => {
    const oldStart = deferred<HostActionResult>();
    const newStart = deferred<HostActionResult>();
    vi.mocked(controlHost)
      .mockReturnValueOnce(oldStart.promise)
      .mockReturnValueOnce(newStart.promise);
    vi.mocked(toast.loading)
      .mockReturnValueOnce("old-progress")
      .mockReturnValueOnce("new-progress");
    const { result, rerender } = renderHook(useSessionReconnect, { initialProps: localSession });
    let first!: Promise<void>;
    let second!: Promise<void>;
    await act(async () => {
      first = result.current.reconnect();
    });
    rerender({ ...localSession, sessionId: "other-session" });
    await act(async () => {
      second = result.current.reconnect();
    });
    expect(controlHost).toHaveBeenCalledTimes(2);
    expect(toast.dismiss).toHaveBeenCalledExactlyOnceWith("old-progress");

    await act(async () => {
      oldStart.resolve({ ok: false, error: "Old failure" });
      await first;
      await result.current.reconnect();
    });
    expect(controlHost).toHaveBeenCalledTimes(2);
    expect(result.current.localReconnect?.reconnecting).toBe(true);
    expect(result.current.localReconnect?.error).toBeNull();
    expect(result.current.dialogOpen).toBe(false);
    expect(toast.dismiss).not.toHaveBeenCalledWith("new-progress");

    await act(async () => {
      newStart.resolve({ ok: true });
      await second;
    });
    expect(result.current.localReconnect?.reconnecting).toBe(false);
    expect(toast.dismiss).toHaveBeenCalledWith("new-progress");
    expect(toast.success).toHaveBeenCalledOnce();
  });
});
