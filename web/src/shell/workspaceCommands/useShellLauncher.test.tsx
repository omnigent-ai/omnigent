import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";

import { useShellLauncher } from "./useShellLauncher";

// Each terminal create's response waits until the test settles it, so it can
// arrive after unmount. The real create hook and TanStack observer run.
interface PendingCreate {
  resolve: (id: string) => void;
  reject: (error: Error) => void;
}
const creates = vi.hoisted(() => [] as PendingCreate[]);

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: () =>
    new Promise((resolve, reject) => {
      creates.push({
        resolve: (id) =>
          resolve({
            ok: true,
            status: 201,
            json: async () => ({
              id,
              object: "session.resource",
              type: "terminal",
              session_id: "conv-1",
              name: `${id}:s1`,
              metadata: { terminal_name: "bash", session_key: "s1", running: true },
            }),
          }),
        reject,
      });
    }),
}));

vi.mock("@/hooks/useAgents", () => ({
  useSessionAgent: () => ({ data: { terminals: ["bash", "zsh"] } }),
}));

function renderLauncher(onOpened = vi.fn(), onCreateError = vi.fn()) {
  const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
  const hook = renderHook(() => useShellLauncher("conv-1", { onOpened, onCreateError }), {
    wrapper,
  });
  return { ...hook, onOpened, onCreateError };
}

/** Wait for `count` creates to reach the network, settle them, then let callbacks run. */
async function settle(count: number, run: () => void) {
  await act(async () => {
    await vi.waitFor(() => expect(creates).toHaveLength(count));
    run();
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
  });
}

beforeEach(() => {
  creates.length = 0;
  localStorage.clear();
});

afterEach(() => localStorage.clear());

describe("useShellLauncher", () => {
  it("opens the new shell's tab once its create succeeds", async () => {
    const { result, onOpened } = renderLauncher();
    act(() => result.current.launch("bash"));

    await settle(1, () => creates[0].resolve("terminal_bash_1"));

    expect(onOpened).toHaveBeenLastCalledWith("terminal:terminal_bash_1");
  });

  it("still opens a shell whose create finishes after the launcher unmounts", async () => {
    const { result, unmount, onOpened } = renderLauncher();
    act(() => result.current.launch("bash"));
    unmount();

    await settle(1, () => creates[0].resolve("terminal_bash_1"));

    expect(onOpened).toHaveBeenLastCalledWith("terminal:terminal_bash_1");
  });

  it("drops a launch made while another create for the session is in flight", async () => {
    const { result } = renderLauncher();
    act(() => result.current.launch("bash"));
    act(() => result.current.launch("zsh"));

    await settle(1, () => creates[0].resolve("terminal_bash_1"));

    expect(creates).toHaveLength(1);
  });

  it("reports a failed create", async () => {
    const { result, onCreateError } = renderLauncher();
    act(() => result.current.launch("bash"));

    await settle(1, () => creates[0].reject(new Error("runner offline")));

    expect(onCreateError).toHaveBeenCalledTimes(1);
  });

  it("makes a remembered shell the default from then on", async () => {
    const { result } = renderLauncher();
    expect(result.current.shells?.defaultShell).toBe("bash");
    act(() => result.current.launch("zsh", { remember: true }));
    expect(result.current.shells?.defaultShell).toBe("zsh");
    await settle(1, () => creates[0].resolve("terminal_zsh_1"));
  });
});
