import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Agent } from "@/hooks/useAgents";
import { ChatHeader } from "./ChatHeader";

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
  todosPanelOpen: false,
  terminalsListOpen: false,
  showShellsTab: false,
  terminalsLength: 0,
  isClaudeNative: false,
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
      </TooltipProvider>
    </MemoryRouter>,
  );
}

afterEach(cleanup);

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

describe("ChatHeader — mobile Shells entry", () => {
  // Renders the header with an active session so the mobile FAB mounts,
  // then opens its dropdown. `hasRailContent` is true so the FAB shows;
  // every drawer/panel flag stays false so the menu isn't suppressed.
  function openMobileMenu(menuOverrides: Partial<typeof mobileMenu> = {}) {
    render(
      <MemoryRouter initialEntries={["/c/conv_1"]}>
        <TooltipProvider>
          <ChatHeader
            sidebarOpen
            onOpenSidebar={() => {}}
            isChildSession={false}
            parentSessionId={undefined}
            conversationId="conv_1"
            boundAgent={undefined}
            canShare={false}
            onShare={() => {}}
            hasAgentInfo={false}
            onAgentInfo={() => {}}
            hasHeaderMenu={false}
            showFilesPanel={false}
            hasRailContent
            rightPanelOpen={false}
            onToggleRightPanel={() => {}}
            mobileMenu={{ ...mobileMenu, ...menuOverrides }}
          />
        </TooltipProvider>
      </MemoryRouter>,
    );
    // Radix DropdownMenu opens on pointerdown, not click.
    fireEvent.pointerDown(screen.getByRole("button", { name: "Open session menu" }), { button: 0 });
  }

  it("shows the Shells entry with no badge when the agent supports shells but none exist", () => {
    // Parity with the desktop rail's Shells tab: the entry is a default
    // entry point (its tap opens the "+ New shell" empty state), so it
    // renders even at zero shells — and like the desktop tab, omits the
    // count badge there (a "0" would read as an error state).
    openMobileMenu({ showShellsTab: true, terminalsLength: 0 });

    const shells = screen.getByRole("menuitem", { name: /Shells/i });
    expect(shells).toBeInTheDocument();
    expect(shells).not.toHaveTextContent("0");
  });

  it("renders the count badge once shells exist", () => {
    openMobileMenu({ showShellsTab: true, terminalsLength: 2 });

    expect(screen.getByRole("menuitem", { name: /Shells/i })).toHaveTextContent("2");
  });

  it("hides the Shells entry when the tab is unavailable", () => {
    // e.g. a claude-native sub-agent, or an agent with no shell access
    // and no shells — the desktop tab is hidden the same way.
    openMobileMenu({ showShellsTab: false, terminalsLength: 0 });

    expect(screen.queryByRole("menuitem", { name: /Shells/i })).toBeNull();
  });

  it("routes a tap to onOpenShells (opens the InlineTerminalsSection drawer)", () => {
    const onOpenShells = vi.fn();
    openMobileMenu({ showShellsTab: true, terminalsLength: 0, onOpenShells });

    fireEvent.click(screen.getByRole("menuitem", { name: /Shells/i }));
    expect(onOpenShells).toHaveBeenCalledTimes(1);
  });
});
