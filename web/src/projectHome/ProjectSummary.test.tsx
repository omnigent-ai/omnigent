import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as kpisApi from "@/lib/projectKpisApi";
import * as statusApi from "@/lib/projectStatusApi";
import * as itemsApi from "@/lib/projectStatusItemsApi";
import * as projectsApi from "@/lib/projectsApi";
import { draftToDefinition } from "./ProjectKpis";
import { ProjectSummary } from "./ProjectSummary";

const projects = vi.hoisted(() => ({
  data: [] as { id: string | null; name: string }[],
  isLoading: false,
  error: null as Error | null,
  refetch: vi.fn(),
}));
vi.mock("@/hooks/useConversations", () => ({ useProjects: () => projects }));
vi.mock("@/lib/projectsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof projectsApi>()),
  getProject: vi.fn(),
}));
vi.mock("@/lib/projectStatusApi", async (importOriginal) => ({
  ...(await importOriginal<typeof statusApi>()),
  getProjectStatusSummary: vi.fn(),
}));
vi.mock("@/lib/projectStatusItemsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof itemsApi>()),
  getProjectStatusItems: vi.fn(),
}));
vi.mock("@/lib/projectKpisApi", async (importOriginal) => ({
  ...(await importOriginal<typeof kpisApi>()),
  getProjectKpis: vi.fn(),
  setProjectKpis: vi.fn(),
}));

function summary(
  overrides: Partial<statusApi.ProjectStatusSummary> = {},
): statusApi.ProjectStatusSummary {
  return {
    object: "project.status_summary",
    project_id: "project",
    window: "7d",
    time_zone: "UTC",
    today_utc: "2026-03-10",
    since_day_utc: "2026-03-04",
    generated_at: 1,
    totals: {
      tickets_done: 3,
      prs_merged: null,
      spend_cents: 1234,
      cost_per_merged_pr_cents: null,
      members: 1,
    },
    members: [],
    sources: { spend: "available", prs_merged: "unavailable" },
    ...overrides,
  };
}

const DAYS = [0, 1, 2, 0, 3, 1, 2, 1].map((value, index) => ({
  day_utc: `2026-03-${String(index + 3).padStart(2, "0")}`,
  value,
}));

function kpi(overrides: Partial<kpisApi.ProjectKpiValue>): kpisApi.ProjectKpiValue {
  return {
    position: 0,
    name: "Shipped",
    metric: "tickets_shipped",
    why: "Throughput.",
    target: 5,
    direction: "at_least",
    not_measured_reason: null,
    metric_label: "Tickets shipped",
    unit: "count",
    value: 10,
    sample_size: null,
    status: "met",
    trend: {
      since_day_utc: "2026-03-03",
      until_day_utc: "2026-03-09",
      days: DAYS,
      last_7_days: 9,
      last_7_days_sample_size: null,
      today: 1,
      status: "met",
    },
    ...overrides,
  };
}

function kpis(overrides: Partial<kpisApi.ProjectKpis> = {}): kpisApi.ProjectKpis {
  return {
    object: "project.kpis",
    project_id: "project",
    window: "7d",
    today_utc: "2026-03-10",
    since_day_utc: "2026-03-04",
    generated_at: 1,
    revision: 0,
    updated_at: null,
    updated_by_user_id: null,
    updated_by_session_id: null,
    can_edit: true,
    kpis: [],
    metrics: [
      { id: "tickets_shipped", label: "Tickets shipped", unit: "count", description: "" },
      { id: "spend_usd", label: "Spend", unit: "usd", description: "" },
    ],
    ...overrides,
  };
}

const clients: QueryClient[] = [];
function renderSummary() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  render(<ProjectSummary projectName="Shipping" />, {
    wrapper: ({ children }) => (
      <MemoryRouter>
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      </MemoryRouter>
    ),
  });
}

beforeEach(() => {
  vi.resetAllMocks();
  projects.data = [{ id: "project", name: "Shipping" }];
  projects.isLoading = false;
  projects.error = null;
  vi.mocked(projectsApi.getProject).mockResolvedValue({ id: "project", name: "Shipping" });
  vi.mocked(statusApi.getProjectStatusSummary).mockResolvedValue(summary());
  vi.mocked(kpisApi.getProjectKpis).mockResolvedValue(kpis());
});

afterEach(() => {
  cleanup();
  for (const client of clients.splice(0)) client.clear();
});

describe("ProjectSummary", () => {
  it("shows a spinner while loading and a retry on error", async () => {
    let fail: (error: Error) => void = () => {};
    vi.mocked(statusApi.getProjectStatusSummary).mockReturnValue(
      new Promise((_, reject) => {
        fail = reject;
      }),
    );
    renderSummary();
    expect(screen.getByLabelText("Loading summary")).toBeTruthy();
    fail(new Error("Server is down"));
    expect(await screen.findByText("Server is down")).toBeTruthy();
    vi.mocked(statusApi.getProjectStatusSummary).mockResolvedValue(summary());
    await userEvent.click(screen.getAllByRole("button", { name: "Try again" })[0]);
    expect(await screen.findByText("$12.34")).toBeTruthy();
  });

  it("marks a metric without a source as not tracked, not zero", async () => {
    renderSummary();
    const headline = await screen.findByRole("region", { name: "Headline numbers" });
    expect(await within(headline).findByText("Not tracked on this server")).toBeTruthy();
    expect(within(headline).getByText("—")).toBeTruthy();
  });

  it("offers the owner to add KPIs when none are set", async () => {
    renderSummary();
    expect(await screen.findByText(/No KPIs yet. Add 3 or 4/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Add KPIs" })).toBeTruthy();
  });

  it("renders one tile per KPI with its 7-day value, today, target and state", async () => {
    vi.mocked(kpisApi.getProjectKpis).mockResolvedValue(
      kpis({
        revision: 2,
        updated_at: 1,
        updated_by_user_id: "owner@example.com",
        can_edit: false,
        kpis: [
          kpi({}),
          kpi({
            position: 1,
            name: "Lead time",
            metric: "time_to_merged_median_hours",
            unit: "hours",
            target: 24,
            direction: "at_most",
            trend: {
              ...kpi({}).trend!,
              last_7_days: 30.5,
              last_7_days_sample_size: 18,
              today: null,
              status: "missed",
            },
          }),
          kpi({
            position: 2,
            name: "NPS",
            metric: "none",
            target: null,
            direction: null,
            unit: null,
            not_measured_reason: "No survey yet.",
            trend: null,
            status: "not_measured",
          }),
        ],
      }),
    );
    renderSummary();
    const tiles = await screen.findAllByTestId("project-kpi-tile");
    expect(tiles).toHaveLength(3);
    const [shipped, leadTime, nps] = tiles;
    expect(within(shipped).getByText("On target")).toBeTruthy();
    expect(within(shipped).getByText("Target: at least 5")).toBeTruthy();
    expect(within(shipped).getByText("9")).toBeTruthy();
    expect(within(shipped).getByTestId("project-kpi-bars").children).toHaveLength(8);
    expect(within(leadTime).getByText("Off target")).toBeTruthy();
    expect(within(leadTime).getByText("30.5 h")).toBeTruthy();
    expect(within(leadTime).getByText("Last 7 days, from 18 tickets")).toBeTruthy();
    expect(within(leadTime).getByText("Target: at most 24 h")).toBeTruthy();
    expect(within(nps).getByText("No survey yet.")).toBeTruthy();
    expect(screen.getByTestId("project-kpi-footer").textContent).toContain(
      "Set by owner@example.com",
    );
    // Members read KPIs but can't edit them.
    const section = screen.getByRole("region", { name: "KPIs" });
    expect(within(section).queryByRole("button", { name: "Edit" })).toBeNull();
  });

  it("saves the owner's edit against the revision it started from", async () => {
    const current = kpis({ revision: 3, updated_at: 1, kpis: [kpi({})] });
    vi.mocked(kpisApi.getProjectKpis).mockResolvedValue(current);
    vi.mocked(kpisApi.setProjectKpis).mockResolvedValue({ ...current, revision: 4 });
    renderSummary();
    const section = await screen.findByRole("region", { name: "KPIs" });
    await userEvent.click(await within(section).findByRole("button", { name: "Edit" }));
    const target = await screen.findByLabelText("KPI 1 target");
    await userEvent.clear(target);
    await userEvent.type(target, "8");
    await userEvent.click(screen.getByTestId("project-kpis-save"));
    expect(kpisApi.setProjectKpis).toHaveBeenCalledWith(
      "project",
      [
        {
          name: "Shipped",
          metric: "tickets_shipped",
          why: "Throughput.",
          target: 8,
          direction: "at_least",
        },
      ],
      3,
      "7d",
    );
  });
});

describe("drill-down", () => {
  const items: itemsApi.ProjectStatusItems = {
    object: "project.status_items",
    project_id: "project",
    metric: "tickets_shipped",
    unit: "count",
    range: "7d",
    member: "user:owner@example.com",
    since_day_utc: "2026-03-04",
    today_utc: "2026-03-10",
    available: true,
    total: 1,
    sample_size: null,
    days: [{ day_utc: "2026-03-10", value: 1, items: 1 }],
    items: [
      {
        day_utc: "2026-03-10",
        at: 1,
        kind: "ticket",
        id: "ticket-a",
        title: "Ship it",
        url: null,
        member: "user:owner@example.com",
        value: null,
        counted_in_total: true,
      },
    ],
    excluded: [
      {
        day_utc: "2026-03-09",
        at: 2,
        kind: "ticket",
        id: "ticket-b",
        title: "Imported",
        url: null,
        member: "user:owner@example.com",
        value: null,
        counted_in_total: true,
        reason: "Created in a completed stage, never moved into it",
      },
    ],
    truncated: false,
  };

  it("opens the items behind a member's cell, with what's left out and why", async () => {
    vi.mocked(statusApi.getProjectStatusSummary).mockResolvedValue(
      summary({
        members: [
          {
            key: "user:owner@example.com",
            kind: "human",
            name: "Owner",
            user_id: "owner@example.com",
            identity_id: null,
            tickets_done: 1,
            prs_merged: null,
            spend_cents: 0,
            cost_per_merged_pr_cents: null,
          },
        ],
      }),
    );
    vi.mocked(itemsApi.getProjectStatusItems).mockResolvedValue(items);
    renderSummary();
    await userEvent.click(
      await screen.findByRole("button", {
        name: "Tickets done by Owner: show the items behind it",
      }),
    );
    const dialog = await screen.findByTestId("project-status-items");
    expect(itemsApi.getProjectStatusItems).toHaveBeenCalledWith(
      "project",
      expect.objectContaining({
        metric: "tickets_shipped",
        range: "7d",
        member: "user:owner@example.com",
      }),
      expect.anything(),
    );
    expect(within(dialog).getByText("Tickets done for Owner")).toBeTruthy();
    expect(within(dialog).getByRole("link", { name: "Ship it" }).getAttribute("href")).toContain(
      "ticket=ticket-a",
    );
    const [left] = within(dialog).getAllByTestId("project-status-excluded-row");
    expect(
      within(left).getByText("Created in a completed stage, never moved into it"),
    ).toBeTruthy();
  });

  it("opens a KPI tile's items over the tile's 8 days", async () => {
    vi.mocked(kpisApi.getProjectKpis).mockResolvedValue(
      kpis({ revision: 1, updated_at: 1, kpis: [kpi({})] }),
    );
    vi.mocked(itemsApi.getProjectStatusItems).mockResolvedValue({ ...items, range: "trend" });
    renderSummary();
    await userEvent.click(await screen.findByRole("button", { name: "See the items" }));
    await screen.findByTestId("project-status-items");
    expect(itemsApi.getProjectStatusItems).toHaveBeenCalledWith(
      "project",
      expect.objectContaining({ metric: "tickets_shipped", range: "trend" }),
      expect.anything(),
    );
  });
});

describe("draftToDefinition", () => {
  const base = {
    name: "N",
    metric: "spend_usd" as const,
    why: "W",
    target: "5",
    direction: "at_most" as const,
    reason: "",
  };
  it("names the first problem to fix", () => {
    expect(draftToDefinition({ ...base, name: " " })).toBe("Give every KPI a name.");
    expect(draftToDefinition({ ...base, target: "" })).toMatch(/target of zero or more/);
    expect(draftToDefinition({ ...base, metric: "none" })).toMatch(/isn't measured yet/);
  });
  it("sends a measured KPI's target and an unmeasured one's reason", () => {
    expect(draftToDefinition(base)).toEqual({
      name: "N",
      metric: "spend_usd",
      why: "W",
      target: 5,
      direction: "at_most",
    });
    expect(draftToDefinition({ ...base, metric: "none", reason: "Later." })).toEqual({
      name: "N",
      metric: "none",
      why: "W",
      not_measured_reason: "Later.",
    });
  });
});
