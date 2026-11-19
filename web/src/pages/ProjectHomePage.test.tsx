import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectHomePage } from "./ProjectHomePage";
import { useProjectComposerStore } from "@/projectHome/ProjectHomeHeader";

const mocks = vi.hoisted(() => ({ isMobile: false, ticketsEnabled: false }));

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
vi.mock("@/projectHome/ProjectTicketBoard", () => ({
  ProjectTicketBoard: () => <div data-testid="ticket-board" />,
}));
vi.mock("@/projectHome/ProjectTicketPanel", () => ({
  ProjectTicketPanel: ({ ticketId, onClose }: { ticketId: string; onClose: () => void }) => (
    <button type="button" onClick={onClose}>
      Close ticket {ticketId}
    </button>
  ),
  ProjectOrchestratorPanel: ({ onClose }: { onClose: () => void }) => (
    <button type="button" onClick={onClose}>
      Close orchestrator
    </button>
  ),
}));

vi.mock("@/projectHome/ProjectKanbanBoard", async () => {
  const { Link } = await import("@/lib/routing");
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
        {quickViewHref && <Link to={quickViewHref("session-1")}>Quick view</Link>}
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
  mocks.isMobile = false;
  mocks.ticketsEnabled = false;
  useProjectComposerStore.setState({ open: false });
});

describe("ProjectHomePage tickets", () => {
  it.each([
    ["ticket=ticket-1", "Ticket details", "Close ticket ticket-1"],
    ["orchestrator=1", "Orchestrator chat", "Close orchestrator"],
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
      expect(screen.getByTestId("location")).toHaveTextContent(/^\/projects\/Payments$/);
    },
  );
});

afterEach(cleanup);

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
