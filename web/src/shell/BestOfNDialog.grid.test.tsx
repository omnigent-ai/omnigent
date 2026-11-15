import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type * as BestOfNModule from "@/lib/bestOfN";
import type * as HostModule from "@/lib/host";
import type * as NativeBridge from "@/lib/nativeBridge";
import type * as SessionsApiModule from "@/lib/sessionsApi";
import { testAgent } from "@/test/agentFixtures";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useBulkArchiveConversations } from "@/hooks/useConversations";
import { GridSessionsProvider } from "@/grid/GridSessionsContext";
import { gridLayoutForSessions, type SessionGrid } from "@/grid/gridLayout";
import { launchBestOfN, type BestOfNLaunchResult } from "@/lib/bestOfN";
import { fetchFirstUserPrompt, getSessionSlim } from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";
import { BestOfNDialog, KeepAttemptDialog } from "./BestOfNDialog";

// The real grid provider and local storage; only the network and the device are stubbed.
vi.mock("@/hooks/useIsMobileViewport", () => ({ useIsMobileViewport: () => false }));
vi.mock("@/hooks/useIsCoarsePointer", () => ({ useIsCoarsePointer: () => false }));
vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridge>()),
  isIOSShell: () => false,
  isAndroidShell: () => false,
}));
vi.mock("@/hooks/useViewerId", () => ({
  useViewerId: () => "alice",
  useIdentityReady: () => true,
}));
vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof HostModule>()),
  getOmnigentServerIdentity: () => "one",
}));
vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
vi.mock("@/hooks/useConversations", () => ({ useBulkArchiveConversations: vi.fn() }));
vi.mock("@/lib/bestOfN", async (importOriginal) => ({
  ...(await importOriginal<typeof BestOfNModule>()),
  launchBestOfN: vi.fn(),
}));
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  fetchFirstUserPrompt: vi.fn(),
  getSessionSlim: vi.fn(),
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() } }));

const KEY = "omnigent:session-grids:one:alice";
const mutateAsync = vi.fn();
const savedGrids = (): SessionGrid[] => JSON.parse(localStorage.getItem(KEY) ?? "[]");
const grid = (id: string, sessionIds: string[], run?: string): SessionGrid => ({
  id,
  ...(run ? { run } : {}),
  layout: gridLayoutForSessions(sessionIds)!,
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

function Location() {
  const location = useLocation();
  return <output data-testid="path">{location.pathname}</output>;
}

function renderAt(path: string, dialog: ReactNode) {
  const page = (
    <GridSessionsProvider>
      {dialog}
      <Location />
    </GridSessionsProvider>
  );
  render(
    <QueryClientProvider client={new QueryClient()}>
      <CapabilitiesProvider
        info={{
          ...FALLBACK_SERVER_INFO,
          sibling_sessions: true,
          features: { best_of_n: true, grid_sessions_display_ui: true },
        }}
      >
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/c/:conversationId" element={page} />
            <Route path="/grid/:gridId" element={page} />
          </Routes>
        </MemoryRouter>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  vi.mocked(useBulkArchiveConversations).mockReturnValue({
    mutateAsync,
  } as unknown as ReturnType<typeof useBulkArchiveConversations>);
  vi.mocked(useAvailableAgents).mockReturnValue({
    data: [
      testAgent("ag_claude", "claude-native-ui", {
        display_name: "Claude Code",
        harness: "claude-native",
      }),
      testAgent("ag_codex", "codex-native-ui", { display_name: "Codex", harness: "codex-native" }),
    ],
  } as unknown as ReturnType<typeof useAvailableAgents>);
  vi.mocked(fetchFirstUserPrompt).mockResolvedValue("Fix the login redirect");
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("keeps the user's own grid and removes the run's grid only after archiving", async () => {
  // Run A/B/C opened its grid; B was archived separately; the user saved A/C.
  localStorage.setItem(
    KEY,
    JSON.stringify([
      grid("run", ["conv_a", "conv_b", "conv_c"], "g"),
      grid("custom", ["conv_a", "conv_c"]),
    ]),
  );
  const labels = {
    "omnigent.best_of_n.group": "g",
    "omnigent.best_of_n.attempts": "conv_a,conv_b,conv_c",
  };
  vi.mocked(getSessionSlim).mockImplementation(
    async (id) =>
      ({
        id,
        title: `Title ${id}`,
        labels,
        permissionLevel: null,
        archived: id === "conv_b",
      }) as unknown as Session,
  );
  const archived = deferred<unknown>();
  mutateAsync.mockReturnValue(archived.promise);
  renderAt(
    "/grid/run",
    <KeepAttemptDialog
      sessionId="conv_a"
      title="Attempt 1"
      labels={labels}
      open
      onOpenChange={vi.fn()}
    />,
  );

  expect(await screen.findByTestId("keep-attempt-others")).toHaveTextContent("Title conv_c");
  fireEvent.click(screen.getByTestId("keep-attempt-confirm"));
  await waitFor(() =>
    expect(mutateAsync).toHaveBeenCalledWith({
      ids: ["conv_c"],
      archived: true,
      deleteWorktreeIds: undefined,
    }),
  );
  expect(savedGrids().map((saved) => saved.id)).toEqual(["run", "custom"]);
  expect(screen.getByTestId("path")).toHaveTextContent("/grid/run");

  archived.resolve([]);
  await waitFor(() => expect(screen.getByTestId("path")).toHaveTextContent("/c/conv_a"));
  expect(savedGrids().map((saved) => saved.id)).toEqual(["custom"]);
});

it.each([true, false])(
  "adds the run's grid to grids saved in another tab while the launch ran (event first: %s)",
  async (eventFirst) => {
    localStorage.setItem(KEY, JSON.stringify([grid("before", ["conv_x", "conv_y"])]));
    const launch = deferred<BestOfNLaunchResult>();
    vi.mocked(launchBestOfN).mockReturnValue(launch.promise);
    renderAt(
      "/c/conv_source",
      <BestOfNDialog
        conversation={{
          id: "conv_source",
          title: "Fix login",
          host_id: "host_1",
          workspace: "/repo",
          agent_id: "ag_codex",
          git_branch: null,
        }}
        open
        onOpenChange={vi.fn()}
      />,
    );
    await waitFor(() => expect(screen.getByTestId("best-of-n-start")).toBeEnabled());
    fireEvent.click(screen.getByTestId("best-of-n-start"));
    await waitFor(() => expect(launchBestOfN).toHaveBeenCalled());

    // Another tab replaces the saved grids while worktrees are still being created;
    // its storage event may reach this tab before or after the launch finishes.
    const external = [grid("other-tab", ["conv_x", "conv_z"])];
    localStorage.setItem(KEY, JSON.stringify(external));
    const notify = () =>
      window.dispatchEvent(new StorageEvent("storage", { key: KEY, storageArea: localStorage }));
    if (eventFirst) notify();
    launch.resolve({ groupId: "g", sessionIds: ["conv_a", "conv_b"], failures: [] });
    if (!eventFirst) notify();

    await waitFor(() => expect(screen.getByTestId("path").textContent).toMatch(/^\/grid\//));
    const saved = savedGrids();
    expect(saved.map((entry) => entry.id)[0]).toBe("other-tab");
    expect(saved).toHaveLength(2);
    expect(saved[1]).toMatchObject({
      name: "Best of 2",
      run: "g",
      layout: gridLayoutForSessions(["conv_a", "conv_b"]),
    });
    expect(screen.getByTestId("path")).toHaveTextContent(`/grid/${saved[1].id}`);
  },
);
