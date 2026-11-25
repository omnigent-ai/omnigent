import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as telemetryApi from "@/lib/projectTelemetryApi";
import type { ProjectTelemetry } from "@/lib/projectTelemetryApi";
import { InsightsPanels, insightsNumbers } from "@/projectHome/ProjectInsights";
import { InsightsPage, totalInsights } from "./InsightsPage";

vi.mock("@/lib/projectTelemetryApi", async (importOriginal) => ({
  ...(await importOriginal<typeof telemetryApi>()),
  getTelemetryOverview: vi.fn(),
}));

function project(name: string, overrides: Partial<ProjectTelemetry> = {}): ProjectTelemetry {
  return {
    object: "project.telemetry",
    project_id: name.toLowerCase(),
    project_name: name,
    window: "30d",
    since_day_utc: "2026-02-09",
    today_utc: "2026-03-10",
    work_start: {
      sessions_started_by_people: 4,
      tickets_filed_directly: 3,
      tickets_filed_from_a_chat: 2,
      tickets_filed_by_agents: 5,
      people: { session_first: 1, ticket_first: 1, both: 2 },
    },
    role_interaction: {
      messages_to_lead: null,
      messages_to_roles: null,
      people_messaging_roles: null,
      side_chats_opened: 2,
      side_chats_with_roles: 1,
      ticket_comments_by_people: 6,
      people_ticket_only: null,
    },
    team_building: {
      roles: 3,
      roles_created_by_lead: 2,
      roles_created_by_hand: 1,
      roles_created_in_window: 1,
      roles_created_in_window_by_lead: 1,
      roles_edited_after_creation: 2,
      median_hours_to_last_role_edit: 6.5,
    },
    throughput: {
      tickets_shipped: 7,
      merged_prs: 5,
      rework_tickets: 1,
      time_to_merged_median_hours: 11,
      spend_usd: 12.5,
      review_rounds_per_shipped_ticket: 1.4,
      stalled_tickets: 2,
    },
    people: null,
    ...overrides,
  };
}

const clients: QueryClient[] = [];
function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  render(<InsightsPage />, {
    wrapper: ({ children }) => (
      <MemoryRouter>
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      </MemoryRouter>
    ),
  });
}

beforeEach(() => vi.resetAllMocks());
afterEach(() => {
  cleanup();
  for (const client of clients.splice(0)) client.clear();
});

describe("InsightsPanels", () => {
  it("shows a dash and a note for numbers that aren't measured", () => {
    render(<InsightsPanels numbers={insightsNumbers(project("Alpha"))} />);
    const roles = screen.getByRole("region", { name: "Lead and roles" });
    expect(within(roles).getAllByText("—").length).toBe(4);
    expect(within(roles).getByText("2")).toBeTruthy(); // side chats are measured
    expect(screen.getByText(/aren't measured yet/)).toBeTruthy();
    const team = screen.getByRole("region", { name: "Building the team" });
    expect(within(team).getByText("Created by the Lead 67%")).toBeTruthy();
    expect(within(team).getByText("6.5 h")).toBeTruthy();
  });
});

describe("totalInsights", () => {
  it("adds counts across projects and leaves out medians and rates", () => {
    const totals = totalInsights([project("Alpha"), project("Beta")]);
    expect(totals.workStart.sessions_started_by_people).toBe(8);
    expect(totals.shipping.tickets_shipped).toBe(14);
    expect(totals.shipping.time_to_merged_median_hours).toBeNull();
    expect(totals.roles.messages_to_lead).toBeNull();
  });
});

describe("InsightsPage", () => {
  it("lists each owned project with a link to its Summary", async () => {
    vi.mocked(telemetryApi.getTelemetryOverview).mockResolvedValue({
      object: "telemetry.overview",
      window: "30d",
      since_day_utc: "2026-02-09",
      today_utc: "2026-03-10",
      projects: [project("Alpha"), project("Beta")],
    });
    renderPage();
    const table = await screen.findByRole("region", { name: "By project" });
    const link = within(table).getByRole("link", { name: "Alpha" });
    expect(link.getAttribute("href")).toBe("/projects/Alpha?tab=summary");
    expect(within(table).getAllByText("2 of 3")).toHaveLength(2);
    expect(telemetryApi.getTelemetryOverview).toHaveBeenCalledWith("30d", expect.anything());
    await userEvent.click(screen.getByRole("button", { name: "7 days" }));
    expect(telemetryApi.getTelemetryOverview).toHaveBeenCalledWith("7d", expect.anything());
  });

  it("explains an empty overview", async () => {
    vi.mocked(telemetryApi.getTelemetryOverview).mockResolvedValue({
      object: "telemetry.overview",
      window: "30d",
      since_day_utc: null,
      today_utc: "2026-03-10",
      projects: [],
    });
    renderPage();
    expect(await screen.findByText(/don.t own any projects yet/)).toBeTruthy();
  });
});
