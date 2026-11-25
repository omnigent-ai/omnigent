import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "@/lib/projectTicketsApi";
import type { ProjectTicket } from "@/lib/projectTicketsApi";
import { workingFor } from "./ProjectTicketBoard";
import { TicketStallFooter } from "./TicketStallFooter";
import { ticketStalled } from "./ticketStall";

vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ project_tickets: true, project_ticket_stages: true }),
}));
const projects = vi.hoisted(() => ({ router: { identity_id: "orchestrator", name: "Lead" } }));
vi.mock("./useProjectAgentSession", () => ({
  useProjectRecord: () => ({ data: { id: "project", name: "Shipping", router: projects.router } }),
}));
vi.mock("@/lib/projectTicketsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof api>()),
  commentOnTicket: vi.fn(),
}));

const NOW_MS = 1_800_000_000_000;
const TWO_HOURS_AGO = NOW_MS / 1000 - 2 * 3600;

const ticket = {
  id: "stuck",
  project_id: "project",
  title: "Inbox filter by project",
  stall: { idle_since: TWO_HOURS_AGO, after_s: 2 * 3600 },
} as ProjectTicket;

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  projects.router = { identity_id: "orchestrator", name: "Lead" };
});

function renderFooter(card: ProjectTicket) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TicketStallFooter ticket={card} nowMs={NOW_MS} />
    </QueryClientProvider>,
  );
}

describe("stalled cards", () => {
  it("read as stalled only once the stage's threshold passes", () => {
    expect(ticketStalled(ticket, NOW_MS)).toBe(true);
    expect(ticketStalled(ticket, NOW_MS - 1000)).toBe(false);
    expect(ticketStalled({ stall: null }, NOW_MS)).toBe(false);
  });

  it("explain the stall and wake the Lead with an @Lead comment", async () => {
    vi.mocked(api.commentOnTicket).mockResolvedValue(ticket);
    renderFooter(ticket);
    expect(screen.getByTestId("ticket-stalled").textContent).toContain(
      "Stalled · no one on this for 2h",
    );
    fireEvent.click(screen.getByRole("button", { name: "Wake the Lead" }));
    await waitFor(() =>
      expect(api.commentOnTicket).toHaveBeenCalledWith("project", "stuck", {
        text: "@Lead this ticket has had no one on it for 2h. Please find out why and get it moving.",
        kind: "comment",
      }),
    );
  });

  it("wake the project's own router when the owner chose another persona", async () => {
    projects.router = { identity_id: "pm", name: "Product Manager" };
    vi.mocked(api.commentOnTicket).mockResolvedValue(ticket);
    renderFooter(ticket);
    fireEvent.click(screen.getByRole("button", { name: "Wake Product Manager" }));
    await waitFor(() =>
      expect(api.commentOnTicket).toHaveBeenCalledWith("project", "stuck", {
        text: "@Product Manager this ticket has had no one on it for 2h. Please find out why and get it moving.",
        kind: "comment",
      }),
    );
  });

  it("show nothing before the threshold", () => {
    renderFooter({ ...ticket, stall: { idle_since: TWO_HOURS_AGO + 60, after_s: 2 * 3600 } });
    expect(screen.queryByTestId("ticket-stalled")).toBeNull();
  });

  it("say how long a worker has been at it", () => {
    expect(workingFor(30)).toBe("");
    expect(workingFor(12 * 60 + 5)).toBe("12m");
    expect(workingFor(65 * 60)).toBe("1h 5m");
  });
});
