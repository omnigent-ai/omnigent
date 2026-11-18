import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { emptyTriggerFields, renderTriggerPrompt } from "@/components/scheduled/triggerTemplates";
import { SCHEDULED_TASKS_KEY } from "@/hooks/useScheduledTasks";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { authenticatedFetch } from "@/lib/identity";
import type * as IdentityModule from "@/lib/identity";
import { formatRunTimestamp } from "@/lib/scheduleText";
import { savedWorkflowMessage } from "@/lib/workflow";
import { WorkflowAutomations } from "./WorkflowAutomations";

// Exercise the real automations query against controlled API responses.
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: vi.fn(),
}));

const WORKFLOW = { id: "0123456789abcdef0123456789abcdef", name: "Triage issues" };
const NOW = new Date("2026-11-12T18:00:00Z");
const LAST_RUN_AT = Date.parse("2026-11-12T09:00:00Z") / 1000;
const DAILY = {
  id: "daily",
  name: "Daily triage",
  // Matched by ID, so an automation follows its workflow through a rename.
  prompt: savedWorkflowMessage("/", { ...WORKFLOW, name: "Old workflow name" }, "", false),
  rrule: "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
  timezone: "UTC",
  state: "active",
  session_mode: "new",
  last_run_at: LAST_RUN_AT,
  last_run_status: "succeeded",
  next_run_at: "2026-11-13T09:00:00Z",
};
const UNRELATED = {
  ...DAILY,
  id: "unrelated",
  name: "Unrelated automation",
  prompt: savedWorkflowMessage("/", { ...WORKFLOW, id: "a".repeat(32) }, "", false),
};
const ISSUE_TRIGGER = {
  ...DAILY,
  id: "issues",
  name: "New issue triage",
  prompt: renderTriggerPrompt(
    "github",
    {
      ...emptyTriggerFields("github"),
      scope: "example/project",
      instructions: savedWorkflowMessage("$", WORKFLOW, "Only urgent issues", false),
    },
    { placement: "same_host", sessionLink: "https://example.test/c/CONVERSATION_ID" },
  ),
  rrule: "FREQ=MINUTELY;INTERVAL=10",
  state: "paused",
  session_mode: "continue",
  last_run_status: "failed",
  next_run_at: null,
};

const automationResponse = (tasks: object[]) =>
  new Response(JSON.stringify({ scheduled_tasks: tasks }));

function renderAutomations(
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
  render(
    <QueryClientProvider client={client}>
      <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, features: { automations_v2: true } }}>
        <MemoryRouter>
          <WorkflowAutomations workflowId={WORKFLOW.id} now={NOW} />
        </MemoryRouter>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
  return client;
}

describe("WorkflowAutomations", () => {
  beforeEach(() => {
    vi.mocked(authenticatedFetch)
      .mockReset()
      .mockImplementation(async () => automationResponse([DAILY, UNRELATED, ISSUE_TRIGGER]));
  });
  afterEach(cleanup);

  it("lists the automations that run the workflow, each linking to its page", async () => {
    renderAutomations();
    const rows = await screen.findAllByTestId("workflow-automation");
    const lastRun = formatRunTimestamp(LAST_RUN_AT, NOW);

    expect(rows.map((row) => within(row).getByRole("link").textContent)).toEqual([
      "Daily triage",
      "New issue triage",
    ]);
    const [daily, trigger] = rows;
    expect(within(daily!).getByRole("link")).toHaveAttribute("href", "/tasks/daily");
    expect(within(daily!).getByTestId("workflow-automation-schedule")).toHaveTextContent(
      `Every day at 9:00 AM · Next run in 15 hours · Last run ${lastRun}`,
    );
    expect(within(daily!).queryByText("Paused")).toBeNull();
    expect(within(trigger!).getByRole("link")).toHaveAttribute("href", "/tasks/issues");
    expect(within(trigger!).getByText("Paused")).toBeInTheDocument();
    const triggerSchedule = within(trigger!).getByTestId("workflow-automation-schedule");
    expect(triggerSchedule).toHaveTextContent(
      `Runs when new items match · Last run failed ${lastRun}`,
    );
    expect(triggerSchedule).not.toHaveTextContent("Every 10 minutes");
    expect(within(trigger!).getByText(/^Last run failed/)).toHaveClass("text-status-red");
  });

  it("keeps loaded rows through a failed refresh, and says when none loaded or none run it", async () => {
    const client = renderAutomations();
    expect(await screen.findAllByTestId("workflow-automation")).toHaveLength(2);

    vi.mocked(authenticatedFetch).mockImplementation(
      async () => new Response(null, { status: 500 }),
    );
    await act(async () => {
      await client.invalidateQueries({ queryKey: SCHEDULED_TASKS_KEY });
      // Query observers hear of the failed refresh on a zero-delay timer.
      await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
      });
    });
    expect(client.getQueryState(SCHEDULED_TASKS_KEY)?.status).toBe("error");
    expect(screen.getAllByTestId("workflow-automation")).toHaveLength(2);
    expect(screen.getByRole("link", { name: "Daily triage" })).toBeVisible();
    expect(screen.queryByTestId("workflow-automations-error")).toBeNull();

    cleanup();
    renderAutomations();
    expect(await screen.findByTestId("workflow-automations-error")).toHaveTextContent(
      "Couldn’t load automations.",
    );

    cleanup();
    vi.mocked(authenticatedFetch).mockImplementation(async () => automationResponse([UNRELATED]));
    renderAutomations();
    expect(await screen.findByTestId("workflow-automations-empty")).toHaveTextContent(
      "No automations yet.",
    );
  });
});
