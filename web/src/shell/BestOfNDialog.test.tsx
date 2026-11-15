import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type * as ReactRouterDomModule from "react-router-dom";
import type * as BestOfNModule from "@/lib/bestOfN";
import type * as SessionsApiModule from "@/lib/sessionsApi";
import type { ReactNode } from "react";
import { toast } from "sonner";
import { testAgent } from "@/test/agentFixtures";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useBulkArchiveConversations } from "@/hooks/useConversations";
import { useGridSessions } from "@/grid/GridSessionsContext";
import { launchBestOfN } from "@/lib/bestOfN";
import { fetchFirstUserPrompt, getSessionSlim } from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";
import { BestOfNDialog, KeepAttemptDialog, canStartBestOfN } from "./BestOfNDialog";

const navigateMock = vi.fn();
vi.mock("react-router-dom", async (importOriginal) => ({
  ...(await importOriginal<typeof ReactRouterDomModule>()),
  useNavigate: () => navigateMock,
}));
vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
vi.mock("@/hooks/useConversations", () => ({ useBulkArchiveConversations: vi.fn() }));
vi.mock("@/grid/GridSessionsContext", () => ({ useGridSessions: vi.fn() }));
vi.mock("@/lib/bestOfN", async (importOriginal) => ({
  ...(await importOriginal<typeof BestOfNModule>()),
  launchBestOfN: vi.fn(),
}));
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  fetchFirstUserPrompt: vi.fn(),
  getSessionSlim: vi.fn(),
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const claude = testAgent("ag_claude", "claude-native-ui", {
  display_name: "Claude Code",
  harness: "claude-native",
});
const codex = testAgent("ag_codex", "codex-native-ui", {
  display_name: "Codex",
  harness: "codex-native",
});
const polly = testAgent("ag_polly", "polly", { display_name: "Polly", harness: "claude-sdk" });

const source = {
  id: "conv_source",
  title: "Fix login",
  host_id: "host_1",
  workspace: "/Users/a/proj-worktrees/login",
  agent_id: "ag_codex",
};
const openGrid = vi.fn();
const deleteRunGrids = vi.fn();
const mutateAsync = vi.fn();

function wrap(children: ReactNode, info: Partial<ServerInfo> = {}) {
  return (
    <QueryClientProvider client={new QueryClient()}>
      <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, ...info }}>
        <MemoryRouter>{children}</MemoryRouter>
      </CapabilitiesProvider>
    </QueryClientProvider>
  );
}

function openSelect(testId: string) {
  const trigger = screen.getByTestId(testId);
  fireEvent.pointerDown(trigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
  fireEvent.click(trigger);
}

beforeEach(() => {
  vi.mocked(useAvailableAgents).mockReturnValue({
    data: [claude, codex, polly],
  } as unknown as ReturnType<typeof useAvailableAgents>);
  vi.mocked(useGridSessions).mockReturnValue({
    grids: [
      { id: "run", layout: { axis: "columns", children: ["conv_a", "conv_b"] } },
      { id: "other", layout: { axis: "columns", children: ["conv_a", "conv_other"] } },
    ],
    enabled: true,
    openGrid,
    deleteRunGrids,
  } as unknown as ReturnType<typeof useGridSessions>);
  vi.mocked(useBulkArchiveConversations).mockReturnValue({
    mutateAsync,
  } as unknown as ReturnType<typeof useBulkArchiveConversations>);
  vi.mocked(fetchFirstUserPrompt).mockResolvedValue("Fix the login redirect");
  openGrid.mockReturnValue(true);
  mutateAsync.mockResolvedValue([]);
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("canStartBestOfN", () => {
  const info = {
    ...FALLBACK_SERVER_INFO,
    sibling_sessions: true,
    features: { best_of_n: true },
  } as ServerInfo;
  const row = { archived: false, host_id: "host_1", workspace: "/repo" };

  it("needs the flag, nested agents, ownership and a host workspace", () => {
    expect(canStartBestOfN(info, row, true)).toBe(true);
    expect(canStartBestOfN(info, row, false)).toBe(false);
    expect(canStartBestOfN("loading", row, true)).toBe(false);
    expect(canStartBestOfN({ ...info, features: {} }, row, true)).toBe(false);
    expect(canStartBestOfN({ ...info, sibling_sessions: false }, row, true)).toBe(false);
    expect(canStartBestOfN(info, { ...row, archived: true }, true)).toBe(false);
    expect(canStartBestOfN(info, { ...row, host_id: null }, true)).toBe(false);
    expect(canStartBestOfN(info, { ...row, workspace: null }, true)).toBe(false);
  });
});

describe("BestOfNDialog", () => {
  it("starts the prefilled task on the chosen agents and opens them in a grid", async () => {
    vi.mocked(launchBestOfN).mockResolvedValue({
      groupId: "g",
      sessionIds: ["conv_a", "conv_b", "conv_c"],
      failures: [{ agentName: "Polly", message: "The task wasn't sent: offline" }],
    });
    const onOpenChange = vi.fn();
    render(wrap(<BestOfNDialog conversation={source} open onOpenChange={onOpenChange} />));

    await waitFor(() =>
      expect(screen.getByTestId("best-of-n-prompt")).toHaveValue("Fix the login redirect"),
    );
    // Defaults: the source's agent, then the next harness.
    expect(screen.getByTestId("best-of-n-attempt-0")).toHaveTextContent("Codex");
    expect(screen.getByTestId("best-of-n-attempt-1")).toHaveTextContent("Claude Code");
    expect(screen.getByText("New worktrees of /Users/a/proj")).toBeVisible();
    // A worktree source's branch is the default base, so attempts build on its commits.
    expect(screen.getByTestId("best-of-n-base-branch")).toHaveValue("login");
    expect(screen.getByRole("dialog")).toHaveTextContent("open side by side in a grid");

    fireEvent.click(screen.getByTestId("best-of-n-add-attempt"));
    openSelect("best-of-n-attempt-2");
    fireEvent.click(screen.getByRole("option", { name: "Polly" }));
    fireEvent.change(screen.getByTestId("best-of-n-base-branch"), { target: { value: "main" } });
    fireEvent.click(screen.getByTestId("best-of-n-start"));

    await waitFor(() =>
      expect(openGrid).toHaveBeenCalledWith(["conv_a", "conv_b", "conv_c"], "Best of 3", "g"),
    );
    expect(launchBestOfN).toHaveBeenCalledWith({
      sourceSessionId: "conv_source",
      hostId: "host_1",
      repository: "/Users/a/proj",
      baseBranch: "main",
      prompt: "Fix the login redirect",
      agents: [codex, claude, polly],
    });
    expect(toast.error).toHaveBeenCalledWith("Polly: The task wasn't sent: offline");
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it("removes extra attempts and keeps at least two", () => {
    render(wrap(<BestOfNDialog conversation={source} open onOpenChange={vi.fn()} />));
    expect(screen.queryByRole("button", { name: /Remove attempt/ })).toBeNull();
    fireEvent.click(screen.getByTestId("best-of-n-add-attempt"));
    fireEvent.click(screen.getByTestId("best-of-n-add-attempt"));
    expect(screen.queryByTestId("best-of-n-add-attempt")).toBeNull();
    expect(screen.getByTestId("best-of-n-start")).toHaveTextContent("Start 4 attempts");
    fireEvent.click(screen.getByRole("button", { name: "Remove attempt 1" }));
    expect(screen.getByTestId("best-of-n-attempt-0")).toHaveTextContent("Claude Code");
    expect(screen.getByTestId("best-of-n-start")).toHaveTextContent("Start 3 attempts");
  });

  it("opens the first attempt when no grid can be saved", async () => {
    openGrid.mockReturnValue(false);
    vi.mocked(useGridSessions).mockReturnValue(null);
    vi.mocked(launchBestOfN).mockResolvedValue({
      groupId: "g",
      sessionIds: ["conv_a", "conv_b"],
      failures: [],
    });
    render(
      wrap(
        <BestOfNDialog
          conversation={{ ...source, workspace: "/repo", git_branch: null }}
          open
          onOpenChange={vi.fn()}
        />,
      ),
    );
    expect(screen.getByRole("dialog")).toHaveTextContent("appear under this session");
    expect(screen.getByTestId("best-of-n-base-branch")).toHaveValue("");
    await waitFor(() => expect(screen.getByTestId("best-of-n-start")).toBeEnabled());
    fireEvent.click(screen.getByTestId("best-of-n-start"));
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_a"));
    expect(launchBestOfN).toHaveBeenCalledWith(expect.objectContaining({ baseBranch: "" }));
  });

  it("stays open with the error when no attempt starts", async () => {
    vi.mocked(launchBestOfN).mockResolvedValue({
      groupId: "g",
      sessionIds: [],
      failures: [{ agentName: "Codex", message: "not a git repository" }],
    });
    const onOpenChange = vi.fn();
    render(wrap(<BestOfNDialog conversation={source} open onOpenChange={onOpenChange} />));
    await waitFor(() => expect(screen.getByTestId("best-of-n-start")).toBeEnabled());
    fireEvent.click(screen.getByTestId("best-of-n-start"));
    expect(await screen.findByRole("alert")).toHaveTextContent("not a git repository");
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(openGrid).not.toHaveBeenCalled();
  });

  it("can't start without a task", async () => {
    vi.mocked(fetchFirstUserPrompt).mockResolvedValue(null);
    render(wrap(<BestOfNDialog conversation={source} open onOpenChange={vi.fn()} />));
    await waitFor(() => expect(fetchFirstUserPrompt).toHaveBeenCalled());
    expect(screen.getByTestId("best-of-n-start")).toBeDisabled();
    fireEvent.change(screen.getByTestId("best-of-n-prompt"), { target: { value: "Do it" } });
    expect(screen.getByTestId("best-of-n-start")).toBeEnabled();
  });
});

describe("KeepAttemptDialog", () => {
  const run = (attempts: string, group = "g") => ({
    "omnigent.best_of_n.group": group,
    "omnigent.best_of_n.attempts": attempts,
  });
  const attempt = (id: string, overrides: Partial<Session> = {}) =>
    ({
      id,
      title: `Title ${id}`,
      labels: run("conv_a,conv_b,conv_c"),
      permissionLevel: null,
      archived: false,
      ...overrides,
    }) as Session;

  function renderKeep(info: Partial<ServerInfo> = {}) {
    render(
      wrap(
        <KeepAttemptDialog
          sessionId="conv_a"
          title="Attempt 1 · Codex"
          labels={run("conv_a,conv_b,conv_c")}
          open
          onOpenChange={vi.fn()}
        />,
        info,
      ),
    );
  }

  it("archives the verified attempts, then drops the run's grid and opens the kept one", async () => {
    vi.mocked(getSessionSlim).mockImplementation(async (id) => attempt(id));
    renderKeep({ archive_worktree_cleanup: true });

    expect(await screen.findByTestId("keep-attempt-others")).toHaveTextContent(
      "Title conv_bTitle conv_c",
    );
    expect(screen.getByTestId("keep-attempt-remove-worktrees")).not.toBeChecked();
    fireEvent.click(screen.getByTestId("keep-attempt-remove-worktrees"));
    fireEvent.click(screen.getByTestId("keep-attempt-confirm"));

    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_a"));
    expect(mutateAsync).toHaveBeenCalledWith({
      ids: ["conv_b", "conv_c"],
      archived: true,
      deleteWorktreeIds: new Set(["conv_b", "conv_c"]),
    });
    expect(toast.success).toHaveBeenCalledWith(
      "Kept Attempt 1 · Codex. Archived 2 other attempts.",
    );
    expect(deleteRunGrids).toHaveBeenCalledExactlyOnceWith("g");
  });

  it("archives only sessions whose own labels place them in the run", async () => {
    vi.mocked(getSessionSlim).mockImplementation(async (id) => {
      if (id === "conv_b") return attempt(id, { labels: run("conv_a,conv_b", "other") });
      throw new Error("not found");
    });
    renderKeep();
    expect(await screen.findByText("No other attempts of this run can be archived.")).toBeVisible();
    expect(screen.getByTestId("keep-attempt-confirm")).toBeDisabled();
  });

  it("stays on the grid when archiving fails", async () => {
    vi.mocked(getSessionSlim).mockImplementation(async (id) => attempt(id));
    mutateAsync.mockRejectedValue(new Error("offline"));
    vi.mocked(useGridSessions).mockReturnValue({
      grids: [{ id: "run", layout: { axis: "columns", children: ["conv_a", "conv_b"] } }],
      openGrid,
      deleteRunGrids,
    } as unknown as ReturnType<typeof useGridSessions>);
    renderKeep();
    await screen.findByTestId("keep-attempt-others");
    fireEvent.click(screen.getByTestId("keep-attempt-confirm"));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Some attempts couldn't be archived. Try again from their menus.",
      ),
    );
    expect(deleteRunGrids).not.toHaveBeenCalled();
    expect(navigateMock).not.toHaveBeenCalled();
    // Without server worktree cleanup there is nothing to opt into.
    expect(screen.queryByTestId("keep-attempt-remove-worktrees")).toBeNull();
    expect(screen.getByTestId("keep-attempt-confirm")).toBeEnabled();
  });
});
