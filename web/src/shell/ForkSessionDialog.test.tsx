import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { TooltipProvider } from "@/components/ui/tooltip";
import { ForkSessionDialog } from "./ForkSessionDialog";
import { forkSession, launchRunner } from "@/lib/sessionsApi";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useSessionAgent } from "@/hooks/useAgents";
import { useHosts, type Host } from "@/hooks/useHosts";
import { useDirectorySessions } from "@/hooks/useDirectorySessions";
import { useRunnerHealthRegistration } from "@/hooks/RunnerHealthProvider";
import { useHostFilesystem } from "@/hooks/useHostFilesystem";

const navigateMock = vi.fn();
vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => navigateMock };
});
vi.mock("@/lib/sessionsApi", () => ({ forkSession: vi.fn(), launchRunner: vi.fn() }));
vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
vi.mock("@/hooks/useAgents", () => ({ useSessionAgent: vi.fn() }));
vi.mock("@/hooks/useHosts", () => ({ useHosts: vi.fn() }));
vi.mock("@/hooks/useDirectorySessions", () => ({ useDirectorySessions: vi.fn() }));
vi.mock("@/hooks/RunnerHealthProvider", () => ({ useRunnerHealthRegistration: vi.fn() }));
vi.mock("@/hooks/useHostFilesystem", () => ({ useHostFilesystem: vi.fn() }));
// The tree browser only mounts when browsing; coding-fork tests rely on the
// directory being prefilled from the source, so the real picker never opens —
// stub it anyway to keep its filesystem fetch out of the test.
vi.mock("./WorkspacePicker", async (importActual) => ({
  ...(await importActual<typeof import("./WorkspacePicker")>()),
  WorkspacePicker: ({ onSelect }: { onSelect: (p: string) => void }) => (
    <button type="button" data-testid="mock-pick-workspace" onClick={() => onSelect("/picked")}>
      pick
    </button>
  ),
}));

const forkSessionMock = vi.mocked(forkSession);
const launchRunnerMock = vi.mocked(launchRunner);
const useAvailableAgentsMock = vi.mocked(useAvailableAgents);
const useSessionAgentMock = vi.mocked(useSessionAgent);
const useHostsMock = vi.mocked(useHosts);
const useDirectorySessionsMock = vi.mocked(useDirectorySessions);
const useRunnerHealthMock = vi.mocked(useRunnerHealthRegistration);
const useHostFilesystemMock = vi.mocked(useHostFilesystem);

function host(overrides: Partial<Host> = {}): Host {
  return {
    host_id: "host_1",
    name: "serena-laptop",
    owner: "serena",
    status: "online",
    ...overrides,
  } as Host;
}

function setHosts(hosts: Host[]): void {
  useHostsMock.mockReturnValue({ data: hosts } as ReturnType<typeof useHosts>);
}

// Source session runs claude-sdk (anthropic). The picker should keep all
// SDK targets plus same-family native (claude-native) and hide the
// cross-family native target (codex-native).
const AVAILABLE_AGENTS = [
  {
    id: "ag_claude_sdk",
    name: "claude",
    display_name: "Claude",
    description: null,
    harness: "claude-sdk",
  },
  {
    id: "ag_claude_native",
    name: "claude-native-ui",
    display_name: "Claude Code",
    description: null,
    harness: "claude-native",
  },
  {
    id: "ag_codex_native",
    name: "codex-native-ui",
    display_name: "Codex",
    description: null,
    harness: "codex-native",
  },
  {
    id: "ag_openai",
    name: "gpt",
    display_name: "GPT",
    description: null,
    harness: "openai-agents",
  },
];

function setAgents(available: typeof AVAILABLE_AGENTS, sourceHarness: string | null): void {
  useAvailableAgentsMock.mockReturnValue({
    data: available,
  } as unknown as ReturnType<typeof useAvailableAgents>);
  useSessionAgentMock.mockReturnValue({
    data: { id: "ag_source", name: "source", harness: sourceHarness },
  } as unknown as ReturnType<typeof useSessionAgent>);
}

function renderDialog(
  props: {
    sourceTitle?: string | null;
    sourceWorkspace?: string | null;
    sourceHostId?: string | null;
    sourceGitBranch?: string | null;
    upToResponseId?: string | null;
  } = { sourceTitle: "My session" },
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidateSpy = vi.spyOn(client, "invalidateQueries");
  const utils = render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <MemoryRouter>
          <ForkSessionDialog
            sourceSessionId="conv_src"
            sourceTitle={props.sourceTitle}
            sourceWorkspace={props.sourceWorkspace}
            sourceHostId={props.sourceHostId}
            sourceGitBranch={props.sourceGitBranch}
            upToResponseId={props.upToResponseId}
            open
            onOpenChange={vi.fn()}
          />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return { ...utils, invalidateSpy };
}

/** Open the Radix agent <Select> (mirrors NewChatDialog.test). */
function openAgentSelect(): void {
  const trigger = screen.getByTestId("fork-session-agent-select");
  fireEvent.pointerDown(trigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
  fireEvent.click(trigger);
}

/** Expand the collapsed "Advanced settings" (working dir + git worktree). */
function openAdvanced(): void {
  fireEvent.click(screen.getByTestId("fork-session-advanced-toggle"));
}

beforeEach(() => {
  forkSessionMock.mockReset();
  launchRunnerMock.mockReset();
  navigateMock.mockReset();
  setAgents(AVAILABLE_AGENTS, "claude-sdk");
  // Defaults for the coding-fork wiring; the non-coding tests don't render
  // these fields but the hooks still run (with isCodingSource false).
  setHosts([host()]);
  useDirectorySessionsMock.mockReturnValue({ data: [] } as unknown as ReturnType<
    typeof useDirectorySessions
  >);
  useRunnerHealthMock.mockReturnValue(new Map());
  useHostFilesystemMock.mockReturnValue({
    data: undefined,
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof useHostFilesystem>);
});

afterEach(cleanup);

describe("ForkSessionDialog", () => {
  it("leaves the name optional, suggesting 'Fork of <title>' as the placeholder", () => {
    renderDialog({ sourceTitle: "My session" });
    // Name lives under Advanced now (optional, prefilled-by-placeholder).
    openAdvanced();
    const input = screen.getByTestId("fork-session-title-input");
    expect(input).toHaveValue("");
    expect(input).toHaveAttribute("placeholder", "Fork of My session");
  });

  it("falls back to a generic placeholder when the source has no title", () => {
    renderDialog({ sourceTitle: null });
    openAdvanced();
    const input = screen.getByTestId("fork-session-title-input");
    expect(input).toHaveValue("");
    expect(input).toHaveAttribute("placeholder", "Name the cloned session");
  });

  it("forks with the edited title, refreshes the list, and navigates into the fork", async () => {
    forkSessionMock.mockResolvedValue({
      id: "conv_fork",
    } as unknown as Awaited<ReturnType<typeof forkSession>>);

    const { invalidateSpy } = renderDialog();

    openAdvanced();
    fireEvent.change(screen.getByTestId("fork-session-title-input"), {
      target: { value: "My clone" },
    });
    fireEvent.click(screen.getByTestId("fork-session-submit"));

    await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
    // No agent switch → agent_id omitted (undefined) so the server keeps
    // the source's agent.
    expect(forkSessionMock).toHaveBeenCalledWith("conv_src", "My clone", undefined, undefined);
    // Session list refreshed so the fork shows in the sidebar, then navigated.
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["conversations"] });
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_fork"));
  });

  it("passes the truncation point through on a 'fork from here' and retitles the dialog", async () => {
    forkSessionMock.mockResolvedValue({
      id: "conv_fork",
    } as unknown as Awaited<ReturnType<typeof forkSession>>);
    renderDialog({ sourceTitle: "My session", upToResponseId: "resp_cut" });

    // A truncated fork renames the dialog so the user knows history after
    // the selected response won't be carried over.
    expect(screen.getByText("Fork from this response")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("fork-session-submit"));

    await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
    // The 4th arg is the truncation point — undefined here would mean the
    // dialog dropped it and the fork silently copied the full history.
    expect(forkSessionMock).toHaveBeenCalledWith("conv_src", undefined, undefined, "resp_cut");
  });

  it("omits the title (server derives it) when the field is cleared", async () => {
    forkSessionMock.mockResolvedValue({
      id: "conv_fork",
    } as unknown as Awaited<ReturnType<typeof forkSession>>);
    renderDialog();

    openAdvanced();
    fireEvent.change(screen.getByTestId("fork-session-title-input"), {
      target: { value: "   " },
    });
    fireEvent.click(screen.getByTestId("fork-session-submit"));

    await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
    // Whitespace-only → undefined so the server applies "Fork of <title>".
    expect(forkSessionMock).toHaveBeenCalledWith("conv_src", undefined, undefined, undefined);
  });

  it("pressing Enter in the title input submits the fork", async () => {
    forkSessionMock.mockResolvedValue({
      id: "conv_fork",
    } as unknown as Awaited<ReturnType<typeof forkSession>>);

    renderDialog();

    openAdvanced();
    fireEvent.keyDown(screen.getByTestId("fork-session-title-input"), {
      key: "Enter",
    });

    await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_fork"));
  });

  it("surfaces the server error inline on failure and does not navigate", async () => {
    forkSessionMock.mockRejectedValue(new Error("403 forbidden"));
    renderDialog();

    fireEvent.click(screen.getByTestId("fork-session-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("fork-session-error")).toHaveTextContent("403 forbidden"),
    );
    // A failed fork must not navigate the user away from the source session.
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it("offers history-preserving targets including cross-family codex-native", () => {
    // Source is claude-sdk (anthropic). Every classifiable target carries
    // history: SDK targets replay the transcript as context, and native
    // targets (claude-native AND codex-native) rebuild their on-disk
    // transcript from the copied Omnigent items — the codex rollout
    // synthesizer writes the session_meta + event_msg records codex
    // ≥ 0.133 needs (verified on 0.136.0), so cross-family codex-native
    // is offered too.
    renderDialog();
    openAgentSelect();

    expect(screen.getByTestId("fork-session-agent-option-same")).toBeInTheDocument();
    expect(screen.getByTestId("fork-session-agent-option-ag_claude_sdk")).toBeInTheDocument();
    // Same-family native (claude-native): rebuild-from-items carries history.
    expect(screen.getByTestId("fork-session-agent-option-ag_claude_native")).toBeInTheDocument();
    // SDK target of a different family still carries history as context.
    expect(screen.getByTestId("fork-session-agent-option-ag_openai")).toBeInTheDocument();
    // Cross-family codex-native: rebuild-from-items carries history.
    expect(screen.getByTestId("fork-session-agent-option-ag_codex_native")).toBeInTheDocument();
  });

  it("excludes the source's own agent so it doesn't duplicate 'Same as source'", () => {
    // Regression: a Claude Code (claude-native) session listed both
    // "Same as source" AND "Claude Code" (the claude-native-ui built-in
    // it's bound to) — the same agent twice. The source's own agent must
    // be hidden from the switch list.
    setAgents(AVAILABLE_AGENTS, "claude-native");
    useSessionAgentMock.mockReturnValue({
      data: { id: "ag_claude_native", name: "claude-native-ui", harness: "claude-native" },
    } as unknown as ReturnType<typeof useSessionAgent>);
    renderDialog();
    openAgentSelect();

    expect(screen.getByTestId("fork-session-agent-option-same")).toBeInTheDocument();
    // The source's own agent (claude-native-ui → "Claude Code") is hidden.
    expect(
      screen.queryByTestId("fork-session-agent-option-ag_claude_native"),
    ).not.toBeInTheDocument();
    // Other same-family targets remain: the claude-sdk agent is still offered.
    expect(screen.getByTestId("fork-session-agent-option-ag_claude_sdk")).toBeInTheDocument();
  });

  it("excludes the source's agent even when it's a '(fork …)' clone", () => {
    // A fork-of-a-fork: the source's bound agent is a session-scoped clone
    // named "<builtin> (fork ag_…)". The dedup must strip that suffix so the
    // built-in it derives from is still hidden (no duplicate of "Same as
    // source"). Source here is databricks_coding_agent (openai-agents).
    const agents = [
      ...AVAILABLE_AGENTS,
      {
        id: "ag_dbx",
        name: "databricks_coding_agent",
        display_name: "databricks_coding_agent",
        description: null,
        harness: "openai-agents",
      },
    ];
    setAgents(agents, "openai-agents");
    useSessionAgentMock.mockReturnValue({
      data: {
        id: "ag_dbx_fork",
        name: "databricks_coding_agent (fork ag_5c78e6a)",
        harness: "openai-agents",
      },
    } as unknown as ReturnType<typeof useSessionAgent>);
    renderDialog();
    openAgentSelect();

    // The built-in the source forked from is hidden despite the name suffix.
    expect(screen.queryByTestId("fork-session-agent-option-ag_dbx")).not.toBeInTheDocument();
    // A same-family SDK target (openai gpt) is still offered.
    expect(screen.getByTestId("fork-session-agent-option-ag_openai")).toBeInTheDocument();
    // Cross-family claude-native is offered for an openai source — the
    // runner rebuilds its transcript from the copied Omnigent items.
    expect(screen.getByTestId("fork-session-agent-option-ag_claude_native")).toBeInTheDocument();
  });

  it("excludes the source's agent and resolves its label for a '(switch …)' clone", () => {
    // The in-place Switch Agent flow names its clone "<builtin> (switch <id>)"
    // (server: cloned_agent_name = f"{name} (switch …)"). The previous
    // single-layer, fork-only strip left that suffix in place, so forking a
    // switched session showed the raw slug as the "same as source" label and
    // re-listed the current built-in as a switch target. agentRootName peels
    // the "(switch …)" layer too.
    setAgents(AVAILABLE_AGENTS, "claude-native");
    useSessionAgentMock.mockReturnValue({
      data: {
        id: "ag_claude_switch",
        name: "claude-native-ui (switch ag_c537b7b)",
        harness: "claude-native",
      },
    } as unknown as ReturnType<typeof useSessionAgent>);
    renderDialog();
    openAgentSelect();

    // Label resolves to the built-in's display name, not the raw suffixed slug.
    expect(screen.getByTestId("fork-session-agent-option-same")).toHaveTextContent("Claude Code");
    // The built-in the source was switched to is hidden (no duplicate).
    expect(
      screen.queryByTestId("fork-session-agent-option-ag_claude_native"),
    ).not.toBeInTheDocument();
    // Other same-family targets remain.
    expect(screen.getByTestId("fork-session-agent-option-ag_claude_sdk")).toBeInTheDocument();
  });

  it("excludes the source's agent for a nested fork-of-a-fork clone", () => {
    // Nested clones accumulate suffixes: "<builtin> (fork a) (fork b)". A
    // single-layer strip would leave "<builtin> (fork a)", miss the match, and
    // re-list the built-in. agentRootName recurses to the root.
    setAgents(AVAILABLE_AGENTS, "claude-native");
    useSessionAgentMock.mockReturnValue({
      data: {
        id: "ag_claude_nested",
        name: "claude-native-ui (fork ag_b629fd6) (fork ag_a286ffe)",
        harness: "claude-native",
      },
    } as unknown as ReturnType<typeof useSessionAgent>);
    renderDialog();
    openAgentSelect();

    expect(screen.getByTestId("fork-session-agent-option-same")).toHaveTextContent("Claude Code");
    expect(
      screen.queryByTestId("fork-session-agent-option-ag_claude_native"),
    ).not.toBeInTheDocument();
  });

  it("passes the chosen agent_id when switching agent", async () => {
    forkSessionMock.mockResolvedValue({
      id: "conv_fork",
    } as unknown as Awaited<ReturnType<typeof forkSession>>);
    renderDialog();

    openAgentSelect();
    fireEvent.click(screen.getByTestId("fork-session-agent-option-ag_claude_native"));
    fireEvent.click(screen.getByTestId("fork-session-submit"));

    await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
    // Switching to a same-family native target forwards agent_id so the
    // server clones that agent and marks the fork for native rebuild. The
    // name was left blank (optional) → undefined so the server derives it.
    expect(forkSessionMock).toHaveBeenCalledWith(
      "conv_src",
      undefined,
      "ag_claude_native",
      undefined,
    );
  });

  it("labels the keep-current option with the source agent's name, not generic text", () => {
    // The source is bound to the claude-sdk built-in ("Claude"). The
    // keep-current option reads "<agent> (same as original session)" so the
    // user sees exactly which agent they're keeping, not opaque "Same as
    // source". The display name is resolved from the catalog by agent id.
    setAgents(AVAILABLE_AGENTS, "claude-sdk");
    useSessionAgentMock.mockReturnValue({
      data: { id: "ag_claude_sdk", name: "claude", harness: "claude-sdk" },
    } as unknown as ReturnType<typeof useSessionAgent>);
    renderDialog();
    openAgentSelect();

    const sameOption = screen.getByTestId("fork-session-agent-option-same");
    expect(sameOption).toHaveTextContent("Claude");
    expect(sameOption).toHaveTextContent("(same as original session)");
  });

  describe("coding source (fork + start)", () => {
    const CODING = {
      sourceTitle: "My session",
      sourceWorkspace: "/repo",
      sourceHostId: "host_1",
    };

    it("hides the host/directory fields for a non-coding source", () => {
      // No source workspace → nothing to bind. Advanced still holds the
      // optional name, but there's no host, working dir, or worktree.
      renderDialog({ sourceTitle: "My session" });
      expect(screen.queryByTestId("fork-session-host-select")).not.toBeInTheDocument();
      expect(screen.queryByTestId("fork-session-reuse-dir-hint")).not.toBeInTheDocument();
      openAdvanced();
      expect(screen.getByTestId("fork-session-title-input")).toBeInTheDocument();
      expect(screen.queryByTestId("fork-session-branch-input")).not.toBeInTheDocument();
    });

    it("shows the host + reuse-dir hint up front, keeping the rest behind Advanced", async () => {
      renderDialog(CODING);
      // Host is top-level (offline host = nothing to run on, surfaced early).
      expect(screen.getByTestId("fork-session-host-select")).toBeInTheDocument();
      // The default — reusing the source directory — is announced inline;
      // hovering/focusing "working directory" reveals the full path in a tooltip.
      expect(screen.getByTestId("fork-session-reuse-dir-hint")).toBeInTheDocument();
      fireEvent.focus(screen.getByTestId("fork-session-reuse-dir-path"));
      expect(await screen.findAllByText("/repo")).not.toHaveLength(0);
      // Name, working directory + git worktree are collapsed until expanded.
      expect(screen.queryByTestId("fork-session-title-input")).not.toBeInTheDocument();
      expect(screen.queryByTestId("fork-session-branch-input")).not.toBeInTheDocument();
      openAdvanced();
      expect(screen.getByTestId("fork-session-title-input")).toBeInTheDocument();
      expect(screen.getByTestId("fork-session-branch-input")).toBeInTheDocument();
    });

    it("on a different host (e.g. non-owner), expands Advanced and needs a directory", () => {
      // Source ran on a host the caller doesn't have (their useHosts only
      // returns host_1) — the cross-host / non-owner case.
      renderDialog({
        sourceTitle: "My session",
        sourceWorkspace: "/owners/repo",
        sourceHostId: "host_other",
      });
      // Defaults to the caller's own online host, not the source's.
      // A different machine → no "reuses the working directory" hint, and the
      // source path isn't prefilled (it's on someone else's box).
      expect(screen.queryByTestId("fork-session-reuse-dir-hint")).not.toBeInTheDocument();
      // Advanced auto-expands so the caller can pick a directory here.
      expect(screen.getByTestId("fork-session-advanced-content")).toBeInTheDocument();
      // Can't start until a directory on this host is chosen.
      expect(screen.getByTestId("fork-session-submit")).toBeDisabled();
    });

    it("surfaces a directory conflict inline WITHOUT auto-expanding on the source host", () => {
      // A connected session already in the source directory → conflict. Cloning
      // a running session always trips this (the original is still there), so it
      // must NOT force Advanced open — the warning shows at the top instead.
      useDirectorySessionsMock.mockReturnValue({
        data: [{ id: "conv_other", host_id: "host_1", workspace: "/repo" }],
      } as unknown as ReturnType<typeof useDirectorySessions>);
      useRunnerHealthMock.mockReturnValue(new Map([["conv_other", true]]));
      renderDialog(CODING);

      // Warning is visible up front…
      expect(screen.getByTestId("fork-session-conflict-hint")).toBeInTheDocument();
      // …but Advanced stays collapsed (same host → no forced expand).
      expect(screen.queryByTestId("fork-session-advanced-content")).not.toBeInTheDocument();
    });

    it("forks, navigates immediately, and fires the runner launch in the background", async () => {
      forkSessionMock.mockResolvedValue({
        id: "conv_fork",
      } as unknown as Awaited<ReturnType<typeof forkSession>>);
      // Launch never resolves: proves navigation does NOT await it (the old
      // awaited flow blocked the modal here — the freeze report).
      launchRunnerMock.mockReturnValue(new Promise(() => {}));

      const { invalidateSpy } = renderDialog(CODING);

      // Host (source host, online) and directory (source workspace) are
      // prefilled, so the button is enabled without further input.
      fireEvent.click(screen.getByTestId("fork-session-submit"));

      await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
      // Name left blank (optional) → undefined so the server derives it.
      expect(forkSessionMock).toHaveBeenCalledWith("conv_src", undefined, undefined, undefined);
      // Navigation happens even though the launch promise is still pending.
      await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_fork"));
      // The launch was kicked off (in the background) on the prefilled host/dir.
      expect(launchRunnerMock).toHaveBeenCalledWith("host_1", "conv_fork", "/repo", undefined);
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["conversations"] });
    });

    it("forwards git worktree options when a branch is named", async () => {
      forkSessionMock.mockResolvedValue({
        id: "conv_fork",
      } as unknown as Awaited<ReturnType<typeof forkSession>>);
      launchRunnerMock.mockResolvedValue({ runnerId: "r1" });

      renderDialog({ ...CODING, sourceGitBranch: "main" });

      // The worktree field lives under Advanced (collapsed by default).
      openAdvanced();
      fireEvent.change(screen.getByTestId("fork-session-branch-input"), {
        target: { value: "feature/x" },
      });
      fireEvent.click(screen.getByTestId("fork-session-submit"));

      await waitFor(() => expect(launchRunnerMock).toHaveBeenCalledTimes(1));
      // Base ref defaults to the source's branch ("main"); the named branch
      // makes the host create an isolated worktree.
      expect(launchRunnerMock).toHaveBeenCalledWith("host_1", "conv_fork", "/repo", {
        branchName: "feature/x",
        baseBranch: "main",
      });
    });

    it("disables the fork button and shows connect-host instructions when no host is online", () => {
      setHosts([host({ status: "offline" })]);
      renderDialog(CODING);

      // Can't start with nothing to run on, and (mirroring NewChatDialog) the
      // CLI reconnect command is surfaced so the user can unblock.
      expect(screen.getByTestId("fork-session-submit")).toBeDisabled();
      expect(screen.queryByTestId("fork-session-host-select")).not.toBeInTheDocument();
      expect(screen.getByTestId("connect-host-command")).toBeInTheDocument();
    });

    it("greys submit when the selected host goes offline on a later refetch", () => {
      // Source host is online and auto-selected on mount → submit enabled.
      setHosts([host({ host_id: "host_1", status: "online" })]);
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      // Fresh element each call so rerender doesn't bail on referential equality.
      const tree = () => (
        <QueryClientProvider client={client}>
          <TooltipProvider>
            <MemoryRouter>
              <ForkSessionDialog
                sourceSessionId="conv_src"
                sourceTitle="My session"
                sourceWorkspace="/repo"
                sourceHostId="host_1"
                open
                onOpenChange={vi.fn()}
              />
            </MemoryRouter>
          </TooltipProvider>
        </QueryClientProvider>
      );
      const { rerender } = render(tree());
      expect(screen.getByTestId("fork-session-submit")).not.toBeDisabled();

      // useHosts refetches and the still-selected host flips offline. canSubmit
      // checks online-ness (not just "a host is selected"), so submit re-greys
      // rather than attempting a launchRunner that would fail server-side.
      setHosts([host({ host_id: "host_1", status: "offline" })]);
      rerender(tree());
      expect(screen.getByTestId("fork-session-submit")).toBeDisabled();
    });

    it("clears the worktree fields when the host changes (no stale base ref)", () => {
      // Two online hosts; source ran on host_1 with branch "main" (prefills
      // the base ref). Switching to host_2 must reset the worktree fields so a
      // base ref from the source machine can't launch a worktree on another.
      setHosts([host({ host_id: "host_1" }), host({ host_id: "host_2", name: "other-laptop" })]);
      renderDialog({ ...CODING, sourceGitBranch: "main" });

      openAdvanced();
      fireEvent.change(screen.getByTestId("fork-session-branch-input"), {
        target: { value: "feature/x" },
      });
      expect(screen.getByTestId("fork-session-branch-input")).toHaveValue("feature/x");

      // Switch host via the Radix Select (mirrors openAgentSelect's gesture).
      const trigger = screen.getByTestId("fork-session-host-select");
      fireEvent.pointerDown(trigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
      fireEvent.click(trigger);
      fireEvent.click(screen.getByTestId("fork-session-host-option-host_2"));

      expect(screen.getByTestId("fork-session-branch-input")).toHaveValue("");
    });

    it("still navigates when the background launch rejects (failure doesn't block)", async () => {
      forkSessionMock.mockResolvedValue({
        id: "conv_fork",
      } as unknown as Awaited<ReturnType<typeof forkSession>>);
      // The launch fails in the background; the dialog must NOT surface it
      // (it has closed + navigated) — recovery is the session page's picker.
      launchRunnerMock.mockRejectedValue(new Error("host busy"));
      renderDialog(CODING);

      fireEvent.click(screen.getByTestId("fork-session-submit"));

      // We navigated into the clone regardless of the launch outcome.
      await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_fork"));
      // Forked exactly once; the dialog showed no inline error (it handed off).
      expect(forkSessionMock).toHaveBeenCalledTimes(1);
      expect(screen.queryByTestId("fork-session-error")).not.toBeInTheDocument();
    });

    it("reveals connect-host instructions via the collapsible toggle when a host is online", () => {
      // Mirrors NewChatDialog: with a host online the picker shows, and the
      // CLI command for connecting ANOTHER host hides behind a toggle.
      renderDialog(CODING);

      expect(screen.queryByTestId("connect-host-command")).not.toBeInTheDocument();
      fireEvent.click(screen.getByTestId("fork-session-connect-host-toggle"));
      expect(screen.getByTestId("connect-host-command")).toBeInTheDocument();
    });
  });
});
