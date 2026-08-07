import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Agent } from "@/hooks/useAgents";
import { ChatHeader } from "./ChatHeader";
import {
  TerminalFirstContextProvider,
  type TerminalFirstContextValue,
} from "./TerminalFirstContext";

// Minimal mobile-menu prop block. All gating booleans are false / counts are
// zero so the mobile FAB and three-dot menu never render — these tests only
// care about the left-slot open-sidebar toggle.
const mobileMenu = {
  fileViewerOpen: false,
  panelOpen: false,
  terminalFirst: false,
  executionLogsOpen: false,
  filesPanelOpen: false,
  subagentsPanelOpen: false,
  shellsPanelOpen: false,
  todosPanelOpen: false,
  hideTerminalsTab: false,
  showShellsTab: false,
  terminalsLength: 0,
  todosSupported: false,
  todosCompleted: 0,
  todosTotal: 0,
  debugMode: false,
  changedCount: 0,
  subagentsWorking: 0,
  agentCount: 1,
  onOpenFiles: () => {},
  onOpenShells: () => {},
  onOpenSubagents: () => {},
  onOpenTodos: () => {},
  onOpenMainExecutionLog: () => {},
};

function renderHeader(props: {
  sidebarOpen: boolean;
  isChildSession?: boolean;
  parentSessionId?: string;
  boundAgent?: Agent;
  wrapperLabel?: string | null;
  canShare?: boolean;
  shareDisabled?: boolean;
  shareDisabledReason?: string;
}) {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <TooltipProvider>
        <ChatHeader
          sidebarOpen={props.sidebarOpen}
          onOpenSidebar={() => {}}
          isChildSession={props.isChildSession ?? false}
          parentSessionId={props.parentSessionId}
          // No active session: PresenceAvatars / AgentInfoButton / right-panel
          // toggle / mobile FAB all gate on conversationId and stay unmounted,
          // isolating the left-slot affordances under test.
          conversationId={undefined}
          boundAgent={props.boundAgent}
          wrapperLabel={props.wrapperLabel ?? null}
          canShare={props.canShare ?? false}
          shareDisabled={props.shareDisabled}
          shareDisabledReason={props.shareDisabledReason}
          onShare={() => {}}
          hasAgentInfo={false}
          onAgentInfo={() => {}}
          hasHeaderMenu={false}
          showFilesPanel={false}
          hasRailContent={false}
          rightPanelOpen={false}
          onToggleRightPanel={() => {}}
          mobileMenu={mobileMenu}
        />
      </TooltipProvider>
    </MemoryRouter>,
  );
}

afterEach(cleanup);

describe("ChatHeader — deployed Share presentation", () => {
  it("matches the compact Vercel action", () => {
    renderHeader({ sidebarOpen: true, canShare: true });

    const share = screen.getByRole("button", { name: "Share session" });
    expect(share).toHaveClass(
      "h-6",
      "gap-1",
      "rounded-[6px]",
      "px-2",
      "text-ui",
      "share-button-glassy",
      "md:inline-flex",
    );
    expect(share).not.toHaveClass("h-8", "rounded-full", "px-6");
    expect(share.querySelector(".lucide-user-plus")).not.toBeNull();
  });

  it("keeps the compact geometry when sharing is disabled", () => {
    renderHeader({
      sidebarOpen: true,
      canShare: true,
      shareDisabled: true,
      shareDisabledReason: "Sharing is unavailable",
    });

    const share = screen.getByRole("button", { name: "Share session" });
    expect(share).toBeDisabled();
    expect(share).toHaveAttribute("title", "Sharing is unavailable");
    expect(share).toHaveClass(
      "h-6",
      "gap-1",
      "rounded-[6px]",
      "px-2",
      "text-ui",
      "share-button-glassy",
    );
    expect(share.querySelector(".lucide-user-plus")).not.toBeNull();
  });
});

describe("ChatHeader — workspace pane alignment", () => {
  it("uses the desktop workspace offset without changing the mobile inset", () => {
    const { container } = renderHeader({ sidebarOpen: true });
    const header = container.querySelector("header");

    expect(header).not.toBeNull();
    expect(header).toHaveClass("inset-x-0", "md:right-[var(--workspace-panel-offset,0px)]");
  });
});

describe("ChatHeader — open-sidebar toggle visibility", () => {
  it("hides the toggle entirely when the sidebar is open", () => {
    renderHeader({ sidebarOpen: true });
    // With the sidebar open there is nothing to open — the toggle must not
    // render at all (its only job is to reopen a closed sidebar).
    expect(screen.queryByRole("button", { name: "Open sidebar" })).toBeNull();
  });

  it("shows the toggle when the sidebar is closed", () => {
    renderHeader({ sidebarOpen: false });
    // Closed: the toggle is the only sidebar affordance, so it must be
    // present. A regression here would hide the only way to reopen the
    // sidebar via pointer.
    expect(screen.getByRole("button", { name: "Open sidebar" })).toBeInTheDocument();
  });
});

describe("ChatHeader — sub-agent affordance", () => {
  it("renders no back link or sub-agent label on a top-level session", () => {
    renderHeader({ sidebarOpen: true, isChildSession: false });
    // Top-level: nothing in the left slot beyond the (hidden) sidebar toggle.
    expect(screen.queryByRole("link", { name: "Back to parent session" })).toBeNull();
    expect(screen.queryByText("Sub-agent")).toBeNull();
  });

  it("links back to the parent and surfaces the bound agent name + caption", () => {
    renderHeader({
      sidebarOpen: true,
      isChildSession: true,
      parentSessionId: "parent-123",
      boundAgent: { id: "a1", name: "check-account-eligibility" },
    });
    // The back affordance must point at the parent session route so the
    // user can climb out of the sub-agent.
    const back = screen.getByRole("link", { name: "Back to parent session" });
    expect(back).toHaveAttribute("href", "/c/parent-123");
    // The agent name proves the bound-agent name reached the header, and
    // the "Sub-agent" caption names the nesting explicitly.
    expect(screen.getByText("check-account-eligibility")).toBeInTheDocument();
    expect(screen.getByText("Sub-agent")).toBeInTheDocument();
  });

  it("names the product, not the internal wrapper row, on a native sub-agent", () => {
    // A Claude Code Task child is bound to its parent's `claude-native-ui`
    // agent — an Omnigent internal the server hides everywhere else
    // (`public_agent_name`). The wrapper label names the product instead.
    renderHeader({
      sidebarOpen: true,
      isChildSession: true,
      parentSessionId: "parent-123",
      boundAgent: { id: "a1", name: "claude-native-ui" },
      wrapperLabel: "claude-code-native-ui-subagent",
    });
    expect(screen.getByText("Claude Code")).toBeInTheDocument();
    expect(screen.queryByText("claude-native-ui")).toBeNull();
  });

  it("falls back to a lone 'Sub-agent' label before the agent snapshot loads", () => {
    renderHeader({
      sidebarOpen: true,
      isChildSession: true,
      parentSessionId: "parent-123",
      boundAgent: undefined,
    });
    // Back link still renders (it only needs the parent id). With no agent
    // name yet, the label collapses to a single "Sub-agent" — never the
    // redundant "Sub-agent" over "Sub-agent" two-line stack.
    expect(screen.getByRole("link", { name: "Back to parent session" })).toHaveAttribute(
      "href",
      "/c/parent-123",
    );
    expect(screen.getByText("Sub-agent")).toBeInTheDocument();
  });
});

function makeTerminalFirstCtx(
  overrides: Partial<TerminalFirstContextValue> = {},
): TerminalFirstContextValue {
  return {
    isClaudeNative: true,
    isNativeWrapper: true,
    isTerminalFirst: true,
    isShellView: false,
    view: "chat",
    terminalViewKey: null,
    setView: () => {},
    terminalsAvailable: true,
    terminalStartingUp: false,
    ...overrides,
  };
}

/**
 * Renders the header with an active session (conversationId set) under the
 * TerminalFirstContext, so the header's Chat/Terminal switcher (ViewModeToggle)
 * mounts. QueryClientProvider covers AgentInfoButton's react-query hooks; it
 * self-hides here (no agent info), leaving the toggle as the asserted control.
 */
function renderHeaderWithSession(ctx: TerminalFirstContextValue | null) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/c/sess-1"]}>
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          {ctx ? (
            <TerminalFirstContextProvider value={ctx}>
              <ChatHeader
                sidebarOpen
                onOpenSidebar={() => {}}
                isChildSession={false}
                parentSessionId={undefined}
                conversationId="sess-1"
                boundAgent={undefined}
                wrapperLabel={null}
                canShare={false}
                onShare={() => {}}
                hasAgentInfo={false}
                onAgentInfo={() => {}}
                hasHeaderMenu={false}
                showFilesPanel={false}
                hasRailContent={false}
                rightPanelOpen={false}
                onToggleRightPanel={() => {}}
                mobileMenu={mobileMenu}
              />
            </TerminalFirstContextProvider>
          ) : (
            <ChatHeader
              sidebarOpen
              onOpenSidebar={() => {}}
              isChildSession={false}
              parentSessionId={undefined}
              conversationId="sess-1"
              boundAgent={undefined}
              wrapperLabel={null}
              canShare={false}
              onShare={() => {}}
              hasAgentInfo={false}
              onAgentInfo={() => {}}
              hasHeaderMenu={false}
              showFilesPanel={false}
              hasRailContent={false}
              rightPanelOpen={false}
              onToggleRightPanel={() => {}}
              mobileMenu={mobileMenu}
            />
          )}
        </TooltipProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("ChatHeader — Chat/Terminal switcher wiring", () => {
  it("mounts the ViewModeToggle for a terminal-first session", () => {
    renderHeaderWithSession(makeTerminalFirstCtx());
    expect(
      screen.getByRole("button", { name: /switch between chat and terminal/i }),
    ).toBeInTheDocument();
  });

  it("omits the toggle for a non-terminal-first session", () => {
    renderHeaderWithSession(makeTerminalFirstCtx({ isTerminalFirst: false }));
    expect(screen.queryByRole("button", { name: /switch between chat and terminal/i })).toBeNull();
  });

  it("omits the toggle when there is no TerminalFirst context", () => {
    renderHeaderWithSession(null);
    expect(screen.queryByRole("button", { name: /switch between chat and terminal/i })).toBeNull();
  });
});
