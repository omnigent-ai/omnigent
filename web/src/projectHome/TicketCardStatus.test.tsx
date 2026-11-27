import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import type * as identity from "@/lib/identity";
import * as api from "@/lib/projectTicketsApi";
import type { ProjectTicket } from "@/lib/projectTicketsApi";
import { workingFor } from "./ProjectTicketBoard";
import { cardStatus, TicketCardStatus } from "./TicketCardStatus";
import { ticketStalled } from "./ticketStall";

vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ project_tickets: true, project_ticket_stages: true }),
}));
const projects = vi.hoisted(() => ({ router: { identity_id: "orchestrator", name: "Lead" } }));
vi.mock("./useProjectAgentSession", () => ({
  useProjectRecord: () => ({ data: { id: "project", name: "Shipping", router: projects.router } }),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof identity>()),
  getCurrentUserId: () => "viewer@example.com",
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
  const view = renderStatus(card);
  const icon = screen.queryByTestId("ticket-stalled");
  if (icon) fireEvent.mouseEnter(icon);
  return view;
}

function renderStatus(card: ProjectTicket) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TicketCardStatus ticket={card} nowMs={NOW_MS} />
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
    expect(screen.getByTestId("ticket-stalled")).toHaveAttribute(
      "aria-label",
      "Stalled · no one on this for 2h",
    );
    expect(screen.getByTestId("ticket-status-popover").textContent).toContain(
      "nothing has changed for 2h",
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

  it("say what waiting work waits on, and never count it as stalled", () => {
    const waiting = (wait: ProjectTicket["wait"], extra: Partial<ProjectTicket> = {}) =>
      cardStatus({ ...ticket, ...extra, stall: null, wait }, NOW_MS, "viewer@example.com");
    expect(waiting({ kind: "sign_off", on: ["risk-ok"] })).toMatchObject({
      kind: "waiting",
      label: "Waiting on risk-ok",
    });
    expect(waiting({ kind: "land", on: [] })).toMatchObject({ label: "Waiting to land" });
    const asked = {
      waiting_on: { reason: "ask", responders: ["viewer@example.com"], raised_by_user_id: null },
    };
    // The card's "Needs …" chip already says so; no icon repeats it until it runs long.
    expect(waiting({ kind: "person", on: [] }, asked)).toBeNull();
    expect(waiting({ kind: "until", on: ["2026-11-27T10:00Z release is out"] })).toMatchObject({
      detail: "release is out",
    });
    renderStatus({ ...ticket, stall: null, wait: { kind: "sign_off", on: ["product-ok"] } });
    expect(screen.getByTestId("ticket-waiting")).toHaveAttribute(
      "aria-label",
      "Waiting on product-ok",
    );
    expect(screen.queryByTestId("ticket-stalled")).toBeNull();
  });

  it("flag a wait that ran too long, count it as stuck and offer Wake", async () => {
    const threeDaysAgo = NOW_MS / 1000 - 3 * 24 * 3600;
    const long = {
      ...ticket,
      stall: null,
      wait: { kind: "land" as const, on: [], since: threeDaysAgo, after_s: 24 * 3600 },
    };
    expect(ticketStalled(long, NOW_MS)).toBe(true);
    expect(ticketStalled({ ...long, wait: { ...long.wait, after_s: null } }, NOW_MS)).toBe(false);
    expect(
      cardStatus(
        { ...long, wait: { kind: "person", on: [], since: threeDaysAgo, after_s: 48 * 3600 } },
        NOW_MS,
        "viewer@example.com",
      ),
    ).toMatchObject({ kind: "long_wait", label: "Waiting on a person · for 3d" });
    vi.mocked(api.commentOnTicket).mockResolvedValue(ticket);
    renderFooter(long);
    expect(screen.getByTestId("ticket-stalled")).toHaveAttribute(
      "aria-label",
      "Waiting to land · for 3d",
    );
    expect(screen.getByTestId("ticket-status-popover").textContent).toContain(
      "Nothing has moved for 3d",
    );
    fireEvent.click(screen.getByRole("button", { name: "Wake the Lead" }));
    await waitFor(() =>
      expect(api.commentOnTicket).toHaveBeenCalledWith("project", "stuck", {
        text: "@Lead this ticket has been waiting to land for 3d. Please find out why and get it moving.",
        kind: "comment",
      }),
    );
  });

  it("say how long a worker has been at it", () => {
    expect(workingFor(30)).toBe("");
    expect(workingFor(12 * 60 + 5)).toBe("12m");
    expect(workingFor(65 * 60)).toBe("1h 5m");
  });
});
