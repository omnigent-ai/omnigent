// The workspace rail's GitHub tab needs a git checkout behind it: for a
// non-git workspace GET /resources/github resolves available:false
// (not_a_git_repo) and the panel is a dead end ("This workspace isn't a git
// repository."). AppShell must key the tab's availability off the resolved
// GitHub info — not just the Files/workspace gate — so the tab is hidden
// (and a remembered GitHub tab selection falls back) once the info resolves
// unavailable.

import type * as UseTerminalsModule from "@/hooks/useTerminals";
import type * as UseChildSessionsModule from "@/hooks/useChildSessions";
import type * as UseSessionModule from "@/hooks/useSession";
import type * as UseConversationsModule from "@/hooks/useConversations";
import type * as UseGithubModule from "@/hooks/useGithub";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";

vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof UseConversationsModule>()),
  useConversations: vi.fn(),
  useStopSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}));
vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  // Keep the real module (inventoryTerminals etc.) — only the
  // network-backed hook is replaced.
  ...(await importOriginal<typeof UseTerminalsModule>()),
  useTerminals: vi.fn(() => ({ terminals: [], isLoading: false, error: null })),
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceEnvironment: vi.fn(() => ({
    data: { available: true, root: null },
    isLoading: false,
  })),
  useWorkspaceChangedFiles: vi.fn(() => ({
    data: { data: [] },
    isSuccess: true,
    isLoading: false,
  })),
}));
vi.mock("@/hooks/useGithub", async (importOriginal) => ({
  // Keep the real module (types, the panel's sibling hooks) — only the
  // info hook AppShell reads is replaced, per-test below.
  ...(await importOriginal<typeof UseGithubModule>()),
  useGithubInfo: vi.fn(() => ({ data: undefined, isLoading: true })),
}));
vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  ...(await importOriginal<typeof UseChildSessionsModule>()),
  useChildSessions: vi.fn(() => ({ children: [], isLoading: false, error: null })),
}));
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSessionModule>()),
  useSession: vi.fn(() => ({ session: null, isLoading: false, error: null })),
}));
vi.mock("@/hooks/useAgents", () => ({
  useSessionAgent: vi.fn(() => ({ data: undefined })),
  useCreateMcpServer: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useUpdateMcpServer: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useDeleteMcpServer: () => ({ mutate: vi.fn(), isPending: false, error: null }),
}));
vi.mock("./Sidebar", () => ({ Sidebar: () => <div data-testid="sidebar" /> }));
vi.mock("./FilesPanel", () => ({
  FilesPanel: () => <div data-testid="files-panel" />,
}));
vi.mock("./FileViewer", () => ({
  FileViewer: () => <div data-testid="file-viewer" />,
}));
vi.mock("./InlineTerminalsSection", () => ({
  InlineTerminalsSection: () => <div data-testid="inline-terminals-section" />,
}));
vi.mock("./FilesPanelDrawer", () => ({
  FilesPanelDrawer: () => <div data-testid="files-panel-drawer" />,
}));
vi.mock("./TerminalsPanel", () => ({
  TerminalsPanel: () => <div data-testid="terminals-panel" />,
}));

import { AppShell } from "./AppShell";
import { useGithubInfo } from "@/hooks/useGithub";
import { useConversations } from "@/hooks/useConversations";

const useGithubInfoMock = vi.mocked(useGithubInfo);

afterEach(cleanup);

beforeEach(() => {
  // The rail persists per-session state (selected tab, width) in
  // localStorage; clear it so one test's writes can't leak into another.
  localStorage.clear();
  sessionStorage.clear();
  useGithubInfoMock.mockReset();
  useGithubInfoMock.mockReturnValue({ data: undefined, isLoading: true } as ReturnType<
    typeof useGithubInfo
  >);
  vi.mocked(useConversations).mockReset();
  vi.mocked(useConversations).mockReturnValue({
    data: {
      pages: [
        {
          data: [
            {
              id: "conv_ws",
              object: "conversation" as const,
              title: null,
              created_at: 0,
              updated_at: 0,
              labels: {},
              permission_level: null,
              host_id: null,
              runner_id: null,
            },
          ],
          first_id: null,
          last_id: null,
          has_more: false,
        },
      ],
      pageParams: [undefined],
    },
  } as never);
});

function renderShell() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/c/conv_ws"]}>
          <Routes>
            <Route element={<AppShell />}>
              <Route path="c/:conversationId" element={<div>chat</div>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

describe("GitHub rail tab visibility", () => {
  it("hides the GitHub tab when the workspace isn't a git repository", () => {
    useGithubInfoMock.mockReturnValue({
      data: { object: "session.github.info", available: false, reason: "not_a_git_repo" },
      isLoading: false,
    } as ReturnType<typeof useGithubInfo>);

    renderShell();

    // The workspace gate is still on: Files renders, so the strip is up.
    expect(screen.getByRole("tab", { name: /^Files$/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /^Agents/ })).toBeInTheDocument();
    // ...but the GitHub tab must not — its panel would be a dead end.
    expect(screen.queryByRole("tab", { name: "GitHub" })).toBeNull();
  });

  it("shows the GitHub tab for a git workspace", () => {
    useGithubInfoMock.mockReturnValue({
      data: { object: "session.github.info", available: true },
      isLoading: false,
    } as ReturnType<typeof useGithubInfo>);

    renderShell();

    expect(screen.getByRole("tab", { name: "GitHub" })).toBeInTheDocument();
  });

  it("keeps the GitHub tab when the host is outdated", () => {
    // An outdated host 404s the info endpoint (reason: host_outdated). The
    // panel renders an actionable "update your host" prompt, so the tab must
    // stay reachable rather than being hidden like the non-git dead end.
    useGithubInfoMock.mockReturnValue({
      data: { object: "session.github.info", available: false, reason: "host_outdated" },
      isLoading: false,
    } as ReturnType<typeof useGithubInfo>);

    renderShell();

    expect(screen.getByRole("tab", { name: "GitHub" })).toBeInTheDocument();
  });

  it("keeps the GitHub tab while the info is still loading (no flash)", () => {
    // Default beforeEach mock: data undefined, isLoading true — matches the
    // Files gate's optimistic default so tabs don't pop in after load.
    renderShell();

    expect(screen.getByRole("tab", { name: "GitHub" })).toBeInTheDocument();
  });

  it("falls back off a remembered GitHub tab when the workspace isn't a git repo", async () => {
    // A session that previously had a git workspace can persist "github" as
    // its selected rail tab; when the tab disappears the selection must
    // converge onto the first still-available tab instead of stranding.
    writeSessionWorkspaceState("conv_ws", { rightRailTab: "github" });
    useGithubInfoMock.mockReturnValue({
      data: { object: "session.github.info", available: false, reason: "not_a_git_repo" },
      isLoading: false,
    } as ReturnType<typeof useGithubInfo>);

    renderShell();

    expect(screen.queryByRole("tab", { name: "GitHub" })).toBeNull();
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: /^Files$/ })).toHaveAttribute("aria-selected", "true"),
    );
  });
});
