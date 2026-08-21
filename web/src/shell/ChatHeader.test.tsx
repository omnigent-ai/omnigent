import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Agent } from "@/hooks/useAgents";
import type { Conversation } from "@/hooks/useConversations";
import type * as NativeBridgeModule from "@/lib/nativeBridge";
import { ChatHeader } from "./ChatHeader";
import {
  TerminalFirstContextProvider,
  type TerminalFirstContextValue,
} from "./TerminalFirstContext";

const { isIOSShellMock, isAndroidShellMock } = vi.hoisted(() => ({
  isIOSShellMock: vi.fn(() => false),
  isAndroidShellMock: vi.fn(() => false),
}));

vi.mock("@/lib/nativeBridge", async (importOriginal) => {
  const actual = await importOriginal<typeof NativeBridgeModule>();
  return {
    ...actual,
    isIOSShell: () => isIOSShellMock(),
    isAndroidShell: () => isAndroidShellMock(),
  };
});

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
  hideTerminalsTab: false,
  showShellsTab: false,
  terminalsLength: 0,
  debugMode: false,
  changedCount: 0,
  subagentsWorking: 0,
  agentCount: 1,
  onOpenFiles: () => {},
  onOpenChanges: () => {},
  onOpenShells: () => {},
  onOpenSubagents: () => {},
  onOpenMainExecutionLog: () => {},
};

function renderHeader(props: {
  sidebarOpen: boolean;
  isChildSession?: boolean;
  conversationId?: string;
  actionConversation?: Conversation | null;
  conversationTitle?: string | null;
  projectName?: string | null;
  titleLinkTo?: string;
  boundAgent?: Agent;
  wrapperLabel?: string | null;
  canShare?: boolean;
  shareDisabled?: boolean;
  shareDisabledReason?: string;
}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <ChatHeader
            sidebarOpen={props.sidebarOpen}
            onOpenSidebar={() => {}}
            isChildSession={props.isChildSession ?? false}
            // Defaults to no active session: PresenceAvatars / AgentInfoButton /
            // right-panel toggle / mobile FAB all gate on conversationId and
            // stay unmounted, isolating the left-slot affordances under test.
            conversationId={props.conversationId}
            actionConversation={props.actionConversation}
            conversationTitle={props.conversationTitle ?? null}
            projectName={props.projectName ?? null}
            titleLinkTo={props.titleLinkTo}
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
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  isIOSShellMock.mockReturnValue(false);
  isAndroidShellMock.mockReturnValue(false);
});

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

describe("ChatHeader — conversation breadcrumb", () => {
  it("renders nothing in the breadcrumb without a resolved title", () => {
    // Landing composer (no conversationId) / snapshot still loading.
    renderHeader({ sidebarOpen: true });
    expect(screen.queryByRole("navigation", { name: "Conversation" })).toBeNull();
  });

  it("shows a plain title (no folder, no link) for an unfiled top-level session", () => {
    renderHeader({
      sidebarOpen: true,
      conversationId: "conv-1",
      conversationTitle: "Fix the login bug",
    });
    const title = screen.getByText("Fix the login bug");
    expect(title).toBeInTheDocument();
    // Top-level has nowhere to climb, so the title is not a link.
    expect(title.closest("a")).toBeNull();
    expect(screen.queryByLabelText(/^Project:/)).toBeNull();
    // No sub-agent segment.
    expect(screen.queryByText("Sub-agent")).toBeNull();
  });

  it("prefixes a folder with the project name in its tooltip when filed", () => {
    renderHeader({
      sidebarOpen: true,
      conversationId: "conv-1",
      conversationTitle: "Fix the login bug",
      projectName: "Payments",
    });
    expect(screen.getByLabelText("Project: Payments")).toBeInTheDocument();
    expect(screen.getByText("Fix the login bug")).toBeInTheDocument();
  });

  it("appends the sub-agent identity and links the title back to the parent", () => {
    renderHeader({
      sidebarOpen: true,
      conversationId: "child-9",
      isChildSession: true,
      conversationTitle: "Fix the login bug",
      titleLinkTo: "/c/parent-123",
      boundAgent: { id: "a1", name: "check-account-eligibility" },
    });
    // The title is the way back out — it links to the parent session route.
    const back = screen.getByRole("link", { name: "Back to parent session" });
    expect(back).toHaveAttribute("href", "/c/parent-123");
    expect(back).toHaveTextContent("Fix the login bug");
    // The sub-agent's bound-agent name is appended on the right.
    expect(screen.getByText("check-account-eligibility")).toBeInTheDocument();
  });

  it("names the product, not the internal wrapper row, on a native sub-agent", () => {
    // A Claude Code Task child is bound to its parent's `claude-native-ui`
    // agent — an Omnigent internal the server hides everywhere else
    // (`public_agent_name`). The wrapper label names the product instead.
    renderHeader({
      sidebarOpen: true,
      conversationId: "child-9",
      isChildSession: true,
      conversationTitle: "Fix the login bug",
      titleLinkTo: "/c/parent-123",
      boundAgent: { id: "a1", name: "claude-native-ui" },
      wrapperLabel: "claude-code-native-ui-subagent",
    });
    expect(screen.getByText("Claude Code")).toBeInTheDocument();
    expect(screen.queryByText("claude-native-ui")).toBeNull();
  });

  it("falls back to a 'Sub-agent' segment before the agent snapshot loads", () => {
    renderHeader({
      sidebarOpen: true,
      conversationId: "child-9",
      isChildSession: true,
      conversationTitle: "Fix the login bug",
      titleLinkTo: "/c/parent-123",
      boundAgent: undefined,
    });
    // Title link still renders (it only needs the parent route); with no agent
    // name yet, the appended segment reads a plain "Sub-agent".
    expect(screen.getByRole("link", { name: "Back to parent session" })).toHaveAttribute(
      "href",
      "/c/parent-123",
    );
    expect(screen.getByText("Sub-agent")).toBeInTheDocument();
  });

  it("renders a Back control instead of the parent title on iOS/Android native", () => {
    isIOSShellMock.mockReturnValue(true);
    renderHeader({
      sidebarOpen: true,
      conversationId: "child-9",
      isChildSession: true,
      conversationTitle: "Fix the login bug",
      titleLinkTo: "/c/parent-123",
    });
    const back = screen.getByRole("link", { name: "Back to parent session" });
    expect(back).toHaveAttribute("href", "/c/parent-123");
    expect(back).toHaveTextContent("Back");
    expect(back).not.toHaveTextContent("Fix the login bug");
    expect(back.querySelector(".lucide-chevron-left")).not.toBeNull();
  });

  it("still links back to the parent when the breadcrumb title is unresolved", () => {
    // Parent outside the loaded sidebar window + snapshot title still null.
    // The climb-out must not wait on a resolved title — titleLinkTo is enough.
    renderHeader({
      sidebarOpen: true,
      conversationId: "child-9",
      isChildSession: true,
      conversationTitle: null,
      titleLinkTo: "/c/parent-123",
    });
    expect(screen.getByRole("link", { name: "Back to parent session" })).toHaveAttribute(
      "href",
      "/c/parent-123",
    );
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
                conversationId="sess-1"
                conversationTitle={null}
                projectName={null}
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
              conversationId="sess-1"
              conversationTitle={null}
              projectName={null}
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
      screen.getByRole("group", { name: /switch between chat and terminal/i }),
    ).toBeInTheDocument();
  });

  it("omits the toggle for a non-terminal-first session", () => {
    renderHeaderWithSession(makeTerminalFirstCtx({ isTerminalFirst: false }));
    expect(screen.queryByRole("group", { name: /switch between chat and terminal/i })).toBeNull();
  });

  it("omits the toggle when there is no TerminalFirst context", () => {
    renderHeaderWithSession(null);
    expect(screen.queryByRole("group", { name: /switch between chat and terminal/i })).toBeNull();
  });
});

describe("ChatHeader — title-adjacent conversation actions", () => {
  const conversation: Conversation = {
    id: "conv-1",
    object: "conversation",
    title: "A very long session title that must truncate before the overflow trigger",
    created_at: 1_700_000_000,
    updated_at: 1_700_000_100,
    labels: {},
    permission_level: 3,
  };

  it("keeps the overflow trigger immediately after a truncating title", () => {
    renderHeader({
      sidebarOpen: true,
      conversationId: conversation.id,
      conversationTitle: conversation.title,
      actionConversation: conversation,
    });

    const title = screen.getByText(conversation.title as string);
    const trigger = screen.getByRole("button", { name: "Conversation actions" });
    expect(title).toHaveClass("min-w-0", "truncate");
    expect(title.parentElement).toBe(trigger.parentElement);
    expect(title.compareDocumentPosition(trigger) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("does not add a management menu to a sub-agent breadcrumb", () => {
    renderHeader({
      sidebarOpen: true,
      conversationId: "child-9",
      isChildSession: true,
      conversationTitle: "Parent session",
      titleLinkTo: "/c/parent-123",
      boundAgent: { id: "a1", name: "reviewer" },
    });

    expect(screen.queryByRole("button", { name: "Conversation actions" })).toBeNull();
    expect(screen.getByRole("link", { name: "Back to parent session" })).toBeInTheDocument();
    expect(screen.getByText("reviewer")).toBeInTheDocument();
  });
});
