import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Agent } from "@/hooks/useAgents";
import type { Conversation } from "@/hooks/useConversations";
import type * as NativeBridgeModule from "@/lib/nativeBridge";
import { setOmnigentHostConfig } from "@/lib/host";
import { ChatHeader } from "./ChatHeader";
import {
  TerminalFirstContextProvider,
  type TerminalFirstContextValue,
} from "./TerminalFirstContext";

const { isIOSShellMock, isAndroidShellMock, isMobileMock } = vi.hoisted(() => ({
  isIOSShellMock: vi.fn(() => false),
  isAndroidShellMock: vi.fn(() => false),
  isMobileMock: vi.fn(() => false),
}));

vi.mock("@/hooks/useIsMobileViewport", () => ({
  useIsMobileViewport: () => isMobileMock(),
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
  subAgentName?: string | null;
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
  hasHeaderMenu?: boolean;
  hasAgentInfo?: boolean;
  hasRailContent?: boolean;
  showFilesPanel?: boolean;
  mobileMenu?: typeof mobileMenu;
  onOpenSidebar?: (peek?: boolean) => void;
}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <ChatHeader
            sidebarOpen={props.sidebarOpen}
            onOpenSidebar={props.onOpenSidebar ?? (() => {})}
            isChildSession={props.isChildSession ?? false}
            subAgentName={props.subAgentName ?? null}
            // Defaults to no active session: PresenceAvatars / AgentInfoButton /
            // right-panel toggle / rail entries all gate on conversationId and
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
            hasAgentInfo={props.hasAgentInfo ?? false}
            onAgentInfo={() => {}}
            hasHeaderMenu={props.hasHeaderMenu ?? false}
            showFilesPanel={props.showFilesPanel ?? false}
            hasRailContent={props.hasRailContent ?? true}
            rightPanelOpen={false}
            onToggleRightPanel={() => {}}
            mobileMenu={props.mobileMenu ?? mobileMenu}
          />
        </TooltipProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  setOmnigentHostConfig({});
  isIOSShellMock.mockReturnValue(false);
  isAndroidShellMock.mockReturnValue(false);
  isMobileMock.mockReturnValue(false);
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

  it("cancels the pending peek as soon as the toggle is pressed", () => {
    vi.useFakeTimers();
    const onOpenSidebar = vi.fn();
    try {
      renderHeader({ sidebarOpen: false, onOpenSidebar });
      const toggle = screen.getByRole("button", { name: "Open sidebar" });

      fireEvent.pointerEnter(toggle);
      fireEvent.pointerDown(toggle);
      act(() => vi.advanceTimersByTime(400));

      expect(onOpenSidebar).not.toHaveBeenCalledWith(true);
      fireEvent.click(toggle);
      expect(onOpenSidebar).toHaveBeenLastCalledWith(false);
    } finally {
      vi.useRealTimers();
    }
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

  it("names the session's own sub-agent, not the parent bundle's agent row", () => {
    // A `sys_session_send` child is bound to its PARENT bundle's agent row,
    // so boundAgent.name is the parent's name. The session's own
    // sub_agent_name must win, or the child masquerades as its parent.
    renderHeader({
      sidebarOpen: true,
      conversationId: "child-9",
      isChildSession: true,
      subAgentName: "comic_one",
      conversationTitle: "Joke night",
      titleLinkTo: "/c/parent-123",
      boundAgent: { id: "a1", name: "joke_director" },
    });
    expect(screen.getByText("comic_one")).toBeInTheDocument();
    expect(screen.queryByText("joke_director")).toBeNull();
  });

  it("falls back past a blank sub_agent_name to the bound agent's name", () => {
    // An empty/whitespace sub_agent_name must not render a blank identity
    // segment — the fallback chain continues to boundAgent.name.
    renderHeader({
      sidebarOpen: true,
      conversationId: "child-9",
      isChildSession: true,
      subAgentName: "  ",
      conversationTitle: "Fix the login bug",
      titleLinkTo: "/c/parent-123",
      boundAgent: { id: "a1", name: "check-account-eligibility" },
    });
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

  it("prefers the vendor product name over sub_agent_name on a native sub-agent", () => {
    // A native child carries BOTH a wrapper label and a sub_agent_name; the
    // product name (matching the Agents rail and composer) outranks the raw
    // sub-agent identifier.
    renderHeader({
      sidebarOpen: true,
      conversationId: "child-9",
      isChildSession: true,
      subAgentName: "general-purpose",
      conversationTitle: "Fix the login bug",
      titleLinkTo: "/c/parent-123",
      boundAgent: { id: "a1", name: "claude-native-ui" },
      wrapperLabel: "claude-code-native-ui-subagent",
    });
    expect(screen.getByText("Claude Code")).toBeInTheDocument();
    expect(screen.queryByText("general-purpose")).toBeNull();
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

describe("ChatHeader — floating mobile controls", () => {
  it("gives the mobile control clusters the same glass surface", () => {
    // The bar paints no background and chat scrolls under it, so each cluster
    // needs its own blurred pill to stay legible over moving content.
    isMobileMock.mockReturnValue(true);
    renderHeader({ sidebarOpen: false, conversationId: "conv-1", canShare: true });

    const toggle = screen.getByRole("button", { name: "Open sidebar" });
    const cluster = screen.getByRole("button", { name: "Share session" }).parentElement;
    for (const surface of [toggle, cluster]) {
      expect(surface).toHaveClass(
        "max-md:rounded-full",
        "max-md:bg-background/70",
        "max-md:backdrop-blur-xl",
        "max-md:backdrop-saturate-150",
      );
    }
    // Nothing to show on the landing composer — the pill must not paint empty.
    expect(cluster).toHaveClass("max-md:empty:hidden");
  });

  it("keeps the two clusters the same size around a lone control", () => {
    // The right pill read visibly larger than the left toggle while it padded
    // its child; with no padding a lone size-10 kebab is the same 40px circle.
    isMobileMock.mockReturnValue(true);
    renderHeader({
      sidebarOpen: false,
      conversationId: "conv-1",
      conversationTitle: "Greeting",
      actionConversation: {
        id: "conv-1",
        object: "conversation",
        title: "Greeting",
        created_at: 1,
        updated_at: 2,
        labels: {},
        permission_level: 3,
      },
    });

    const toggle = screen.getByRole("button", { name: "Open sidebar" });
    const trigger = screen.getByRole("button", { name: "Conversation actions" });
    expect(trigger.parentElement).not.toHaveClass("max-md:px-1", "max-md:py-1");
    expect(trigger).toHaveClass("size-10");
    expect(toggle).toHaveClass("size-10");
  });

  it("insets the pill's leading edge for the Chat/Terminal track", () => {
    // The track paints its own background to its edge, so with the pill's
    // zero padding it sat flush against the border while an icon-only
    // neighbour cleared it by the slack in its 40px box. The inset is
    // conditional: a lone kebab must stay the 40px circle asserted above.
    isMobileMock.mockReturnValue(true);
    renderHeaderWithSession(makeTerminalFirstCtx());

    const cluster = screen.getByTestId("view-mode-toggle").parentElement;
    expect(cluster).toHaveClass("max-md:has-data-[slot=view-mode-toggle]:pl-1.5");
    // The guard keys off the track's own data-slot, so it has to be present.
    expect(screen.getByTestId("view-mode-toggle")).toHaveAttribute("data-slot", "view-mode-toggle");
  });

  it("rounds the kebab's own background so no square shows inside the pill", () => {
    // The ghost button paints `aria-expanded:bg-muted` at its rounded-lg
    // radius, which showed as a square behind the round pill once open.
    isMobileMock.mockReturnValue(true);
    renderHeader({
      sidebarOpen: true,
      conversationId: "conv-1",
      canShare: true,
      hasHeaderMenu: true,
    });

    expect(screen.getByTestId("session-actions-menu")).toHaveClass("max-md:rounded-full");
  });

  it("gives the fallback menu the same glass as the controls", () => {
    isMobileMock.mockReturnValue(true);
    renderHeader({
      sidebarOpen: true,
      conversationId: "conv-1",
      canShare: true,
      hasHeaderMenu: true,
    });

    fireEvent.pointerDown(screen.getByTestId("session-actions-menu"), { button: 0 });
    expect(screen.getByRole("menu")).toHaveClass(
      "max-md:bg-background/70",
      "max-md:backdrop-blur-xl",
    );
  });

  it("keeps the page touchable while the fallback menu is open on mobile", () => {
    // Modal mode's body-wide pointer-events:none leaves the menu as the only
    // touch target, so touch-target adjustment snaps outside taps onto it and
    // a phone can never dismiss the menu (see HeaderConversationMenu).
    isMobileMock.mockReturnValue(true);
    renderHeader({
      sidebarOpen: true,
      conversationId: "conv-1",
      canShare: true,
      hasHeaderMenu: true,
    });

    fireEvent.pointerDown(screen.getByTestId("session-actions-menu"), { button: 0 });
    expect(screen.getByRole("menu")).toBeInTheDocument();
    expect(document.body.style.pointerEvents).not.toBe("none");
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
    expect(trigger.closest("nav.conversation-breadcrumb")).not.toHaveClass("group/breadcrumb");
    expect(title.compareDocumentPosition(trigger) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("moves the trigger out of the breadcrumb on mobile", () => {
    isMobileMock.mockReturnValue(true);
    isIOSShellMock.mockReturnValue(true);
    renderHeader({
      sidebarOpen: true,
      conversationId: conversation.id,
      conversationTitle: conversation.title,
      actionConversation: conversation,
    });

    // The native shells hide the breadcrumb, so the kebab has to live in the
    // header's own control cluster to stay reachable.
    const trigger = screen.getByRole("button", { name: "Conversation actions" });
    expect(trigger.closest("nav.conversation-breadcrumb")).toBeNull();
    // Rightmost control in the header's action cluster.
    expect(trigger.parentElement?.lastElementChild).toBe(trigger);
    expect(screen.getByText(conversation.title as string)).toBeInTheDocument();
  });

  it("keeps the mobile Share / Agent info analytics on the owner-managed path", () => {
    // The owner-managed session hides the legacy Share · Agent info menu that
    // used to report these ids, so the kebab it yields to must report them.
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    isMobileMock.mockReturnValue(true);
    renderHeader({
      sidebarOpen: true,
      conversationId: conversation.id,
      conversationTitle: conversation.title,
      actionConversation: conversation,
      canShare: true,
      hasHeaderMenu: true,
      hasAgentInfo: true,
    });

    const openKebab = () =>
      fireEvent.pointerDown(screen.getByRole("button", { name: "Conversation actions" }), {
        button: 0,
      });

    openKebab();
    fireEvent.click(screen.getByRole("menuitem", { name: "Share" }));
    openKebab();
    fireEvent.click(screen.getByRole("menuitem", { name: "Agent info" }));

    expect(analytics.mock.calls.map(([event]) => event.componentId)).toEqual([
      "chat.header.mobile_share",
      "chat.header.mobile_agent_info",
    ]);
  });

  it("renders a single kebab on mobile, not one per menu", () => {
    // The legacy Share · Agent info menu must yield to the full session menu,
    // or a phone header ends up with two adjacent three-dot triggers.
    isMobileMock.mockReturnValue(true);
    renderHeader({
      sidebarOpen: true,
      conversationId: conversation.id,
      conversationTitle: conversation.title,
      actionConversation: conversation,
      canShare: true,
      hasHeaderMenu: true,
      hasAgentInfo: true,
    });

    expect(screen.getByRole("button", { name: "Conversation actions" })).toBeInTheDocument();
    expect(screen.queryByTestId("session-actions-menu")).toBeNull();
  });

  it("falls back to the Share · Agent info menu when the session isn't owner-managed", () => {
    isMobileMock.mockReturnValue(true);
    renderHeader({
      sidebarOpen: true,
      conversationId: conversation.id,
      conversationTitle: conversation.title,
      actionConversation: null,
      canShare: true,
      hasHeaderMenu: true,
      hasAgentInfo: true,
    });

    expect(screen.queryByRole("button", { name: "Conversation actions" })).toBeNull();
    expect(screen.getByTestId("session-actions-menu")).toBeInTheDocument();
  });

  it("folds the workspace-rail entries into the one mobile kebab", () => {
    // Previously a second `PanelRight` trigger sat beside the kebab; the rail
    // entries now ride in the same menu, so a phone has a single trigger.
    isMobileMock.mockReturnValue(true);
    renderHeader({
      sidebarOpen: true,
      conversationId: conversation.id,
      conversationTitle: conversation.title,
      actionConversation: conversation,
      hasRailContent: true,
      showFilesPanel: true,
    });

    expect(screen.queryByRole("button", { name: "Open session menu" })).toBeNull();
    fireEvent.pointerDown(screen.getByRole("button", { name: "Conversation actions" }), {
      button: 0,
    });

    expect(screen.getAllByRole("menuitem").map((item) => item.textContent?.trim())).toEqual([
      "Pin",
      "Rename",
      "Mark as unread",
      "Add to project",
      "Files",
      "Changes",
      "Agents1",
      "Archive",
      "Delete",
    ]);
  });

  it("routes a rail entry from the kebab to its drawer", () => {
    const onOpenFiles = vi.fn();
    isMobileMock.mockReturnValue(true);
    renderHeader({
      sidebarOpen: true,
      conversationId: conversation.id,
      conversationTitle: conversation.title,
      actionConversation: conversation,
      hasRailContent: true,
      showFilesPanel: true,
      mobileMenu: { ...mobileMenu, onOpenFiles },
    });

    fireEvent.pointerDown(screen.getByRole("button", { name: "Conversation actions" }), {
      button: 0,
    });
    fireEvent.click(screen.getByRole("menuitem", { name: "Files" }));
    expect(onOpenFiles).toHaveBeenCalled();
  });

  it("keeps the rail entries reachable when the session isn't owner-managed", () => {
    // No owner menu and nothing else to offer: the fallback kebab must still
    // render, or the drawers have no mobile entry point at all.
    isMobileMock.mockReturnValue(true);
    renderHeader({
      sidebarOpen: true,
      conversationId: conversation.id,
      conversationTitle: conversation.title,
      actionConversation: null,
      hasHeaderMenu: false,
      hasRailContent: true,
      showFilesPanel: true,
    });

    fireEvent.pointerDown(screen.getByTestId("session-actions-menu"), { button: 0 });
    expect(screen.getByRole("menuitem", { name: "Files" })).toBeInTheDocument();
  });

  it("drops the rail entries while a push panel owns the right side", () => {
    isMobileMock.mockReturnValue(true);
    renderHeader({
      sidebarOpen: true,
      conversationId: conversation.id,
      conversationTitle: conversation.title,
      actionConversation: conversation,
      hasRailContent: true,
      showFilesPanel: true,
      mobileMenu: { ...mobileMenu, filesPanelOpen: true },
    });

    fireEvent.pointerDown(screen.getByRole("button", { name: "Conversation actions" }), {
      button: 0,
    });
    expect(screen.queryByRole("menuitem", { name: "Files" })).toBeNull();
    expect(screen.getByRole("menuitem", { name: "Pin" })).toBeInTheDocument();
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
