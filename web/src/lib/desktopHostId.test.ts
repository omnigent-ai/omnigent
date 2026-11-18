import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { resetDesktopHostIdForTests, useDesktopHostId } from "./desktopHostId";

interface FakeShell {
  answer: (identity: { hostId: string } | null) => Promise<void>;
  emitHostStatusChanged: () => void;
  getHostIdentity: ReturnType<typeof vi.fn>;
}

// A desktop shell whose identity reads resolve only when the test answers them.
function installShell(): FakeShell {
  let resolveRead: (identity: { hostId: string } | null) => void = () => {};
  let statusListener: () => void = () => {};
  const getHostIdentity = vi.fn(
    () =>
      new Promise<{ hostId: string } | null>((resolve) => {
        resolveRead = resolve;
      }),
  );
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    getHostIdentity,
    onHostStatusChanged: (listener: () => void) => {
      statusListener = listener;
      return () => {};
    },
  };
  return {
    getHostIdentity,
    answer: (identity) => act(async () => resolveRead(identity)),
    emitHostStatusChanged: () => statusListener(),
  };
}

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  resetDesktopHostIdForTests();
});

describe("useDesktopHostId", () => {
  it("is null in a browser, where there's no shell to ask", () => {
    const { result } = renderHook(() => useDesktopHostId());
    expect(result.current).toBeNull();
  });

  it("is null until the shell answers, then this machine's id", async () => {
    const shell = installShell();
    const { result } = renderHook(() => useDesktopHostId());
    expect(result.current).toBeNull();
    await shell.answer({ hostId: "host_local" });
    expect(result.current).toBe("host_local");
  });

  it("asks the shell once however many components read the id", async () => {
    const shell = installShell();
    const first = renderHook(() => useDesktopHostId());
    const second = renderHook(() => useDesktopHostId());
    await shell.answer({ hostId: "host_local" });
    expect(shell.getHostIdentity).toHaveBeenCalledTimes(1);
    expect(first.result.current).toBe("host_local");
    expect(second.result.current).toBe("host_local");
  });

  it("re-reads the id when the host's status changes", async () => {
    const shell = installShell();
    const { result } = renderHook(() => useDesktopHostId());
    await shell.answer(null);
    expect(result.current).toBeNull();

    shell.emitHostStatusChanged();
    await shell.answer({ hostId: "host_first_connect" });
    expect(shell.getHostIdentity).toHaveBeenCalledTimes(2);
    expect(result.current).toBe("host_first_connect");
  });
});
