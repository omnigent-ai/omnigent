import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectHomePage } from "./ProjectHomePage";
import { resetSharedWidthStoreForTesting } from "@/hooks/useResizablePanel";
import { writePanelSizePreference } from "@/lib/panelSizePreferences";
import { useProjectComposerStore } from "@/projectHome/ProjectHomeHeader";
import { TabsList, TabsTrigger } from "@/components/ui/tabs";

const mocks = vi.hoisted(() => ({ isMobile: false, ticketsEnabled: false }));
const originalInnerWidth = window.innerWidth;

vi.mock(import("@/hooks/useIsMobileViewport"), async (importOriginal) => ({
  ...(await importOriginal()),
  useIsMobileViewport: () => mocks.isMobile,
}));
vi.mock("@/shell/Sidebar", () => ({ useProjectFolderMenu: vi.fn() }));
vi.mock("@/shell/NewChatDialog", () => ({
  NewChatLandingScreen: ({
    projectName,
    onSessionStarted,
  }: {
    projectName: string;
    onSessionStarted: () => void;
  }) => (
    <button type="button" onClick={() => onSessionStarted()}>
      Start session in {projectName}
    </button>
  ),
}));
vi.mock("@/projectHome/useProjectTickets", () => ({
  useProjectTicketsEnabled: () => mocks.ticketsEnabled,
}));
vi.mock("@/projectHome/ProjectViewNavigation", () => ({
  ProjectViewNavigation: () => (
    <TabsList aria-label="Project views">
      <TabsTrigger value="chat">Chat</TabsTrigger>
      <TabsTrigger value="board">Board</TabsTrigger>
    </TabsList>
  ),
}));
vi.mock("@/projectHome/ProjectChat", () => ({
  ProjectChat: () => <div data-testid="project-chat">Maestro conversation</div>,
}));
vi.mock("@/projectHome/ProjectTicketBoard", () => ({
  ProjectTicketBoard: () => (
    <div data-testid="ticket-board">
      <Link to="?ticket=ticket-1">Open ticket ticket-1</Link>
    </div>
  ),
}));
vi.mock("@/projectHome/ProjectTicketPanel", () => ({
  ProjectTicketPanel: ({
    ticketId,
    onClose,
    expanded,
    onExpandedChange,
  }: {
    ticketId: string;
    onClose: () => void;
    expanded?: boolean;
    onExpandedChange?: (expanded: boolean) => void;
  }) => (
    <>
      <input aria-label="Ticket draft" />
      <Link to={`?ticket=${ticketId}&session=worker`}>Chat with worker</Link>
      <Link to={`?ticket=${ticketId}`}>Back to ticket</Link>
      <Link to="?ticket=ticket-2">Related ticket</Link>
      {onExpandedChange && (
        <button type="button" onClick={() => onExpandedChange(!expanded)}>
          {expanded ? "Restore ticket panel" : "Expand ticket"}
        </button>
      )}
      <button type="button" onClick={onClose}>
        Close ticket {ticketId}
      </button>
    </>
  ),
  ProjectOrchestratorPanel: ({ onClose }: { onClose: () => void }) => (
    <button type="button" onClick={onClose}>
      Close orchestrator
    </button>
  ),
}));

vi.mock("@/projectHome/ProjectKanbanBoard", async () => {
  const { Link: ProjectLink } = await import("@/lib/routing");
  return {
    ProjectKanbanBoard: ({
      quickViewHref,
      selectedSessionId,
    }: {
      quickViewHref?: (conversationId: string) => string;
      selectedSessionId: string | null;
    }) => (
      <>
        <output data-testid="selected-card">{selectedSessionId ?? "none"}</output>
        {quickViewHref && <ProjectLink to={quickViewHref("session-1")}>Quick view</ProjectLink>}
      </>
    ),
  };
});

vi.mock("@/projectHome/ProjectSessionPanel", () => ({
  ProjectSessionPanel: ({ sessionId, onClose }: { sessionId: string; onClose: () => void }) => (
    <button type="button" onClick={onClose}>
      Close {sessionId}
    </button>
  ),
}));

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>;
}

function renderPage(entry: string) {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/projects/:projectName" element={<ProjectHomePage />} />
      </Routes>
      <LocationProbe />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: 1440 });
  mocks.isMobile = false;
  mocks.ticketsEnabled = false;
  writePanelSizePreference("projectSessionPanelWidthPx", null);
  resetSharedWidthStoreForTesting();
  useProjectComposerStore.setState({ open: false });
});

describe("ProjectHomePage tickets", () => {
  it.each(["", "?orchestrator=1"])(
    "opens the project conversation for %s and switches to Board",
    (search) => {
      mocks.ticketsEnabled = true;
      renderPage(`/projects/Payments${search}`);
      expect(screen.getByTestId("project-chat")).toBeVisible();
      expect(screen.getByRole("tab", { name: "Chat" })).toHaveAttribute("aria-selected", "true");
      expect(screen.queryByRole("complementary")).toBeNull();
      fireEvent.mouseDown(screen.getByRole("tab", { name: "Board" }), { button: 0 });
      expect(screen.getByTestId("ticket-board")).toBeVisible();
      expect(screen.getByTestId("location")).toHaveTextContent("?view=board");
      fireEvent.mouseDown(screen.getByRole("tab", { name: "Chat" }), { button: 0 });
      expect(screen.getByTestId("project-chat")).toBeVisible();
      expect(screen.getByTestId("location")).toHaveTextContent("?view=chat");
    },
  );

  it("opens a ticket's main-chat context without mounting a duplicate conversation", () => {
    mocks.ticketsEnabled = true;
    renderPage("/projects/Payments?view=chat&ticket=ticket-1");
    expect(screen.queryByTestId("project-chat")).toBeNull();
    expect(screen.getByRole("complementary", { name: "Ticket details" })).toBeVisible();
  });

  it.each(["", "agent=primary", "orchestrator=1"])(
    "does not mount a second lead conversation behind a ticket's %s chat",
    (target) => {
      mocks.ticketsEnabled = true;
      renderPage(`/projects/Payments?view=chat&ticket=ticket-1&${target}`);
      expect(screen.queryByTestId("project-chat")).toBeNull();
      expect(screen.getByRole("complementary", { name: "Ticket details" })).toHaveStyle({
        width: "100%",
      });
    },
  );
  it.each(["ticket-1", "new"])(
    "expands %s in place and restores the resized panel, board, and draft",
    (ticketId) => {
      mocks.ticketsEnabled = true;
      renderPage(`/projects/Payments?ticket=${ticketId}`);
      const panel = screen.getByRole("complementary", { name: "Ticket details" });
      const board = screen.getByTestId("ticket-board");
      const draft = screen.getByRole("textbox", { name: "Ticket draft" });
      fireEvent.change(draft, { target: { value: "Keep this draft" } });
      fireEvent.keyDown(screen.getByRole("separator"), { key: "ArrowLeft" });
      const width = panel.style.width;
      expect(width).toMatch(/px$/);

      fireEvent.click(screen.getByRole("button", { name: "Expand ticket" }));
      expect(panel).toHaveStyle({ width: "100%" });
      expect(board).not.toBeVisible();
      expect(screen.queryByRole("separator")).toBeNull();
      expect(draft).toHaveValue("Keep this draft");
      expect(screen.getByRole("textbox", { name: "Ticket draft" })).toBe(draft);

      fireEvent.click(screen.getByRole("link", { name: "Chat with worker" }));
      expect(screen.getByTestId("location")).toHaveTextContent("session=worker");
      fireEvent.click(screen.getByRole("link", { name: "Back to ticket" }));
      expect(panel).toHaveStyle({ width: "100%" });
      expect(draft).toHaveValue("Keep this draft");

      fireEvent.click(screen.getByRole("button", { name: "Restore ticket panel" }));
      expect(panel).toHaveStyle({ width });
      expect(board).toBeVisible();
      expect(screen.getByTestId("ticket-board")).toBe(board);
      expect(screen.getByRole("separator")).toBeInTheDocument();
      expect(screen.getByRole("textbox", { name: "Ticket draft" })).toBe(draft);
      expect(draft).toHaveValue("Keep this draft");
    },
  );

  it("lets nested overlays take Escape before restoring, then closing the ticket", () => {
    mocks.ticketsEnabled = true;
    renderPage("/projects/Payments?ticket=ticket-1");
    fireEvent.click(screen.getByRole("button", { name: "Expand ticket" }));
    const panel = screen.getByRole("complementary", { name: "Ticket details" });
    const overlay = render(<div role="dialog" aria-modal="true" aria-label="Review ticket" />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(panel).toHaveStyle({ width: "100%" });
    overlay.unmount();

    act(() => useProjectComposerStore.getState().setOpen(true));
    fireEvent.keyDown(screen.getByTestId("project-floating-composer"), { key: "Escape" });
    expect(screen.queryByTestId("project-floating-composer")).toBeNull();
    expect(panel).toHaveStyle({ width: "100%" });

    fireEvent.keyDown(window, { key: "Escape" });
    expect(panel.style.width).not.toBe("100%");
    expect(screen.getByTestId("location")).toHaveTextContent("?ticket=ticket-1");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("complementary")).toBeNull();
  });

  it("resets expansion when closing or switching tickets", () => {
    mocks.ticketsEnabled = true;
    renderPage("/projects/Payments?ticket=ticket-1");
    fireEvent.click(screen.getByRole("button", { name: "Expand ticket" }));
    fireEvent.click(screen.getByRole("button", { name: "Close ticket ticket-1" }));
    fireEvent.click(screen.getByRole("link", { name: "Open ticket ticket-1" }));
    expect(screen.getByRole("button", { name: "Expand ticket" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Expand ticket" }));
    fireEvent.click(screen.getByRole("link", { name: "Related ticket" }));
    expect(screen.getByTestId("location")).toHaveTextContent("?ticket=ticket-2");
    expect(screen.getByRole("button", { name: "Expand ticket" })).toBeInTheDocument();
    expect(screen.getByTestId("ticket-board")).toBeVisible();
  });

  it("keeps mobile tickets full width without a redundant expansion control", () => {
    mocks.ticketsEnabled = true;
    mocks.isMobile = true;
    renderPage("/projects/Payments?ticket=ticket-1");
    expect(screen.getByRole("complementary", { name: "Ticket details" })).toHaveStyle({
      width: "100%",
    });
    expect(screen.getByTestId("ticket-board")).not.toBeVisible();
    expect(screen.queryByRole("button", { name: "Expand ticket" })).toBeNull();
    expect(screen.queryByRole("separator")).toBeNull();
  });

  it.each([
    ["ticket=ticket-1", "Ticket details", "Close ticket ticket-1"],
    ["session=session-1", "Session quick view", "Close session-1"],
  ])(
    "opens %s beside the board and preserves the normal New session composer",
    (search, panel, close) => {
      mocks.ticketsEnabled = true;
      renderPage(`/projects/Payments?${search}`);
      expect(screen.getByTestId("ticket-board")).toBeInTheDocument();
      expect(screen.getByRole("complementary", { name: panel })).toBeInTheDocument();
      act(() => useProjectComposerStore.getState().setOpen(true));
      fireEvent.click(screen.getByRole("button", { name: "Start session in Payments" }));
      expect(screen.queryByTestId("project-floating-composer")).toBeNull();
      expect(screen.getByTestId("location")).toHaveTextContent(`/projects/Payments?${search}`);
      fireEvent.click(screen.getByRole("button", { name: close }));
      expect(screen.queryByRole("complementary")).toBeNull();
      expect(screen.getByTestId("location")).toHaveTextContent(
        /^\/projects\/Payments\?view=board$/,
      );
    },
  );
});

afterEach(() => {
  cleanup();
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    writable: true,
    value: originalInnerWidth,
  });
  writePanelSizePreference("projectSessionPanelWidthPx", null);
  resetSharedWidthStoreForTesting();
});

describe("ProjectHomePage session quick view", () => {
  it("opens a card's session beside the board and closes it from the panel or Escape", () => {
    renderPage("/projects/Payments");
    expect(screen.getByTestId("selected-card")).toHaveTextContent("none");

    fireEvent.click(screen.getByRole("link", { name: "Quick view" }));
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/projects/Payments?session=session-1",
    );
    expect(screen.getByTestId("selected-card")).toHaveTextContent("session-1");

    fireEvent.click(screen.getByRole("button", { name: "Close session-1" }));
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/projects\/Payments$/);
    expect(screen.queryByRole("button", { name: "Close session-1" })).toBeNull();

    fireEvent.click(screen.getByRole("link", { name: "Quick view" }));
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    expect(screen.queryByRole("button", { name: "Close session-1" })).toBeNull();
  });

  it("leaves phones on the full session page, with no quick view", () => {
    mocks.isMobile = true;
    renderPage("/projects/Payments?session=session-1");
    expect(screen.queryByRole("link", { name: "Quick view" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Close session-1" })).toBeNull();
    expect(screen.getByTestId("selected-card")).toHaveTextContent("none");
  });
});
