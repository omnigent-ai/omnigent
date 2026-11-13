// The relay loads its action code only in the desktop app, and reports (then
// retries) a failed load rather than leaving the agent waiting.
import { renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { beforeEach, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ desktop: true, loads: 0, failLoads: 0 }));
const dispatchAgentBrowserAction = vi.hoisted(() => vi.fn());

vi.mock(import("@/lib/nativeBridge"), async (importOriginal) => ({
  ...(await importOriginal()),
  isElectronShell: () => state.desktop,
  supportsBrowser: () => state.desktop,
  getHostIdentity: () => Promise.resolve({ cliInstalled: true, hostId: "host_laptop" }),
}));
const authenticatedFetch = vi.fn();
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: (...args: unknown[]) => authenticatedFetch(...args),
}));
vi.mock("@/components/BrowserPane/agentTools/relayActions", () => {
  state.loads += 1;
  if (state.failLoads > 0) {
    state.failLoads -= 1;
    throw new Error("Failed to fetch dynamically imported module");
  }
  return { dispatchAgentBrowserAction };
});

import { emitBrowserActionRequest } from "@/lib/browserActionBus";
import { RELAY_LOAD_ERROR, useBrowserAgentRelay } from "./useBrowserAgentRelay";

const CONV = "conv_lazy";

function renderRelay() {
  const client = new QueryClient();
  return renderHook(() => useBrowserAgentRelay(CONV), {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
}

function resultPosts(): Record<string, unknown>[] {
  return authenticatedFetch.mock.calls
    .filter((c) => String(c[0]).includes("/browser/action_result/"))
    .map((c) => JSON.parse((c[1] as { body: string }).body) as Record<string, unknown>);
}

async function act(actionId: string) {
  const before = resultPosts().length;
  emitBrowserActionRequest(
    { type: "browser_action_request", actionId, action: "snapshot", args: {} },
    CONV,
  );
  await vi.waitFor(() => expect(resultPosts()).toHaveLength(before + 1));
  return resultPosts().at(-1)?.result;
}

beforeEach(() => {
  authenticatedFetch.mockReset().mockResolvedValue({
    ok: true,
    json: () => Promise.resolve({ claimed: true, claim_token: "tok" }),
  });
  dispatchAgentBrowserAction.mockReset().mockResolvedValue({ ok: true, data: { done: 1 } });
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = {
    browserPerformAction: vi.fn(),
  };
});

it("doesn't load the action code outside the desktop app", () => {
  state.desktop = false;
  const { unmount } = renderRelay();
  expect(state.loads).toBe(0);
  unmount();
  state.desktop = true;
});

// Every load fails here: whether the test runner imports a failed module
// again varies, so the retry itself is covered by loadOnce's own test.
it("starts loading on registering, and reports a failed load to the agent", async () => {
  state.failLoads = Infinity;
  const { unmount } = renderRelay();
  // Before any action.
  await vi.waitFor(() => expect(state.loads).toBeGreaterThan(0));
  expect(await act("baction_1")).toMatchObject({ ok: false, error: RELAY_LOAD_ERROR });
  expect(dispatchAgentBrowserAction).not.toHaveBeenCalled();
  unmount();
});
