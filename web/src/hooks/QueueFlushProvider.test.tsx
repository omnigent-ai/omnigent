import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";

import type * as RunnerHealthModule from "./RunnerHealthProvider";
import type * as HostsModule from "./useHosts";
import type * as SessionModule from "./useSession";
import type * as NativeBridgeModule from "@/lib/nativeBridge";
import { QueueFlushProvider } from "./QueueFlushProvider";
import { initChatStore, useChatStore } from "@/store/chatStore";
import { connectArcaHost, controlHost, getHostIdentity, isElectronShell } from "@/lib/nativeBridge";
import type { HostActionResult } from "@/lib/nativeBridge";

const server = vi.hoisted(() => ({
  hostId: "host_arca",
  hostName: "e2e's arca",
  online: true,
  permissionLevel: 4,
}));

// Server snapshots/liveness and desktop process control are external to the queue.
vi.mock("./useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionModule>()),
  useSession: (id: string) => ({
    session: {
      id,
      hostId: server.hostId,
      permissionLevel: server.permissionLevel,
      title: "Recover this session",
    },
    isLoading: false,
    error: null,
  }),
}));
vi.mock("./useHosts", async (importOriginal) => ({
  ...(await importOriginal<typeof HostsModule>()),
  useHosts: () => ({
    data: [{ host_id: server.hostId, name: server.hostName }],
    isLoading: false,
    error: null,
  }),
}));
vi.mock("./RunnerHealthProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof RunnerHealthModule>()),
  useSessionHostOnline: () => server.online,
}));
vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridgeModule>()),
  connectArcaHost: vi.fn(),
  controlHost: vi.fn(),
  getHostIdentity: vi.fn(),
  isElectronShell: vi.fn(),
}));
vi.mock("sonner", () => ({
  toast: { loading: vi.fn(), success: vi.fn(), dismiss: vi.fn() },
}));

beforeEach(() => {
  vi.resetAllMocks();
  server.hostId = "host_arca";
  server.hostName = "e2e's arca";
  server.online = true;
  server.permissionLevel = 4;
  vi.mocked(isElectronShell).mockReturnValue(true);
  vi.mocked(getHostIdentity).mockResolvedValue({ cliInstalled: true, hostId: "this-mac" });
  useChatStore.setState({ queuedMessages: [], conversationId: "original-session" });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  useChatStore.setState({ queuedMessages: [], conversationId: null });
});

describe("QueueFlushProvider host recovery", () => {
  it("keeps a second offline refusal held without restarting the host again", async () => {
    const queryClient = new QueryClient();
    onTestFinished(() => queryClient.clear());
    initChatStore(queryClient);
    queryClient.setQueryData(["session", "original-session"], { status: "idle" });
    // The server can remain unavailable after the desktop reports successful startup.
    const post = vi.fn(async () =>
      Response.json(
        { error: { code: "host_offline", message: "Waiting for the host." } },
        { status: 503 },
      ),
    );
    vi.stubGlobal("fetch", post);
    vi.mocked(connectArcaHost)
      .mockResolvedValueOnce({ ok: true })
      .mockReturnValue(new Promise(() => {}));
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <StrictMode>
            <QueueFlushProvider>
              <span>Another session stays open</span>
            </QueueFlushProvider>
          </StrictMode>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    act(() => {
      useChatStore.setState({
        conversationId: "another-session",
        queuedMessages: [
          {
            queueId: "held",
            stableId: "same-message",
            conversationId: "original-session",
            text: "Continue here",
            waitingForHost: true,
          },
        ],
      });
    });
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    await act(async () => {});
    expect(useChatStore.getState().queuedMessages[0]).toMatchObject({
      stableId: "same-message",
      waitingForHost: true,
    });
    expect(connectArcaHost).toHaveBeenCalledOnce();
  });

  it.each([
    { hostId: "host_arca", hostName: "e2e's arca", arca: true },
    { hostId: "this-mac", hostName: "This Mac", arca: false },
  ])("recovers a late offline refusal after leaving $hostName", async ({ arca, ...host }) => {
    Object.assign(server, host);
    let finish!: (result: HostActionResult) => void;
    const pending = new Promise<HostActionResult>((resolve) => {
      finish = resolve;
    });
    vi.mocked(controlHost).mockReturnValue(pending);
    vi.mocked(connectArcaHost).mockReturnValue(pending);
    const queryClient = new QueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <StrictMode>
            <QueueFlushProvider>
              <span>Another session stays open</span>
            </QueueFlushProvider>
          </StrictMode>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const file = new File(["notes"], "notes.txt");
    // The send's refusal arrives after the route has switched.
    act(() => {
      useChatStore.setState({ conversationId: "another-session" });
      useChatStore.setState({
        queuedMessages: [
          {
            queueId: "held",
            stableId: "same-message",
            conversationId: "original-session",
            text: "Continue here",
            files: [file],
            waitingForHost: true,
          },
        ],
      });
    });
    await waitFor(() => expect(arca ? connectArcaHost : controlHost).toHaveBeenCalledOnce());
    expect(useChatStore.getState().queuedMessages[0]?.waitingForHost).toBe(true);
    await act(async () => {
      finish({ ok: true });
    });
    await waitFor(() =>
      expect(useChatStore.getState().queuedMessages[0]?.waitingForHost).toBe(false),
    );
    expect(useChatStore.getState().queuedMessages[0]).toMatchObject({
      stableId: "same-message",
      conversationId: "original-session",
      files: [file],
      text: "Continue here",
    });
    expect(useChatStore.getState().conversationId).toBe("another-session");
    queryClient.clear();
  });
});
