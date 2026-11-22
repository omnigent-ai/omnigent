import type { ComponentProps } from "react";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CreateScheduledTaskDialog } from "@/components/scheduled/CreateScheduledTaskDialog";
import {
  LEGACY_MAX_INSTRUCTIONS_CHARS,
  emptyTriggerFields,
  encodeTriggerPrompt,
  type TriggerCondition,
  type TriggerSource,
} from "@/components/scheduled/triggerTemplates";
import { SCHEDULED_TASKS_KEY } from "@/hooks/useScheduledTasks";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { SERVER_INFO_OFFLINE_FALLBACK } from "@/lib/bootCapabilities";
import { basenamedRouting, RoutingProvider } from "@/lib/routing";
import { authenticatedFetch } from "@/lib/identity";
import type * as IdentityModule from "@/lib/identity";
import type { ScheduledTask } from "@/lib/scheduledTasksApi";
import { ProjectTicketSources, projectTicketSourcePreset } from "./ProjectTicketSources";

vi.mock("@/components/scheduled/CreateScheduledTaskDialog", () => ({
  CreateScheduledTaskDialog: vi.fn(
    ({ open, onOpenChange, initialName }: ComponentProps<typeof CreateScheduledTaskDialog>) =>
      open ? (
        <div role="dialog" aria-label="New automation">
          <p>{initialName}</p>
          <button type="button" onClick={() => onOpenChange(false)}>
            Cancel
          </button>
        </div>
      ) : null,
  ),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: vi.fn(),
}));

const INFO = {
  ...SERVER_INFO_OFFLINE_FALLBACK,
  automations_enabled: true,
  scheduled_task_projects: true,
  scheduled_task_session_modes: true,
  features: { automations_v2: true, session_orchestration: true },
};

const SOURCES = [
  ["jira", "Jira board", "Jira"],
  ["slack", "Slack channel", "Slack"],
  ["linear", "Linear team", "Linear"],
  ["github", "GitHub repository", "GitHub"],
  ["pagerduty", "PagerDuty service", "PagerDuty"],
] as const;

const clients: QueryClient[] = [];
function CurrentPage() {
  return <span data-testid="current-page">{useLocation().pathname}</span>;
}

function renderSources(info: typeof INFO | "loading" = INFO, projectId: string | null = "proj_1") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/workspace/projects/Shipping"]}>
        <RoutingProvider value={basenamedRouting("/workspace")}>
          <CapabilitiesProvider info={info}>
            <ProjectTicketSources projectId={projectId} projectName="Shipping" />
            <CurrentPage />
          </CapabilitiesProvider>
        </RoutingProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, client };
}

function sourceButton() {
  return screen.getByRole("button", { name: /^(Connect source|Sources(:.*)?)$/ });
}

function openSources() {
  return userEvent.click(sourceButton());
}

function sourceRecord(
  id: string,
  source: TriggerSource,
  scope: string,
  conditions: TriggerCondition[] = [],
) {
  return {
    id,
    name: `${source} ${scope}`,
    prompt: encodeTriggerPrompt(
      {
        ...emptyTriggerFields(source),
        source,
        scope,
        conditions,
        instructions: "File actionable project tickets.",
        worker: {
          placement: "same_host",
          agentId: null,
          model: "",
          effort: "",
          permissionMode: "",
        },
      },
      { sessionLink: "https://example.test/c/CONVERSATION_ID" },
    ),
    project_id: "proj_1",
    session_mode: "continue",
    rrule: "FREQ=MINUTELY;INTERVAL=10",
    timezone: "UTC",
    state: "active",
    last_run_status: null,
  };
}

function mockSources(records: object[]) {
  vi.mocked(authenticatedFetch).mockImplementation(
    async () => new Response(JSON.stringify({ scheduled_tasks: records })),
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockSources([]);
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
});

describe("project ticket sources", () => {
  it.each(SOURCES)("opens the %s preset without creating anything", async (id, label, service) => {
    renderSources();
    expect(CreateScheduledTaskDialog).not.toHaveBeenCalled();
    await openSources();
    const picker = screen.getByRole("dialog", { name: "Connect a source" });
    expect(within(picker).getAllByRole("button")).toHaveLength(SOURCES.length);
    expect(picker).toHaveAccessibleDescription("Create tickets from external sources.");
    expect(screen.queryByText("No sources linked yet.")).not.toBeInTheDocument();
    expect(screen.queryByText(/You can add multiple boards/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: label }));
    await screen.findByRole("dialog", { name: "New automation" });
    const props = vi.mocked(CreateScheduledTaskDialog).mock.lastCall?.[0];
    expect(props).toMatchObject({
      open: true,
      initialTrigger: id,
      initialName: `${service} → Shipping tickets`,
      initialProject: { id: "proj_1", name: "Shipping" },
      initialPrompt: expect.stringContaining("sys_ticket_create"),
      sessionLink: expect.stringContaining("/workspace/c/CONVERSATION_ID"),
    });
    expect(props?.initialPrompt).toContain("sys_ticket_comment");
    expect(props?.initialPrompt).toContain("canonical source URL");
    expect(props?.initialPrompt).toContain(
      "preserve its current stage, labels, and human decisions",
    );
    expect(props?.initialPrompt?.length).toBeLessThan(LEGACY_MAX_INSTRUCTIONS_CHARS);
    expect(vi.mocked(authenticatedFetch).mock.calls).toEqual([["/v1/scheduled-tasks"]]);
  });

  it("discards a canceled source selection and opens the next preset", async () => {
    renderSources();
    await openSources();
    await userEvent.click(screen.getByRole("button", { name: "Jira board" }));
    await screen.findByRole("dialog", { name: "New automation" });
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await openSources();
    await userEvent.click(screen.getByRole("button", { name: "Slack channel" }));
    await screen.findByRole("dialog", { name: "New automation" });
    expect(vi.mocked(CreateScheduledTaskDialog).mock.lastCall?.[0]).toMatchObject({
      initialTrigger: "slack",
      initialName: "Slack → Shipping tickets",
    });
  });

  it("passes a label-only project without creating it on selection", async () => {
    renderSources(INFO, null);
    await openSources();
    await userEvent.click(screen.getByRole("button", { name: "Linear team" }));
    await screen.findByRole("dialog", { name: "New automation" });
    expect(vi.mocked(CreateScheduledTaskDialog).mock.lastCall?.[0].initialProject).toEqual({
      id: null,
      name: "Shipping",
    });
  });

  it.each([
    ["loading", "loading"],
    ["automations", { ...INFO, automations_enabled: false }],
    ["projects", { ...INFO, scheduled_task_projects: false }],
    ["session continuity", { ...INFO, scheduled_task_session_modes: false }],
    ["orchestration", { ...INFO, features: { ...INFO.features, session_orchestration: false } }],
  ] as const)("does not offer unusable setup without %s", (_label, info) => {
    renderSources(info);
    expect(screen.queryByRole("region", { name: "Project sources" })).not.toBeInTheDocument();
    expect(CreateScheduledTaskDialog).not.toHaveBeenCalled();
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("keeps multiple sources, including two of the same provider, while another is added", async () => {
    const records = [
      sourceRecord("slack_support", "slack", "support"),
      { ...sourceRecord("slack_bugs", "slack", "bugs"), state: "paused" },
      { ...sourceRecord("jira", "jira", "OPS"), last_run_status: "failed" },
      { ...sourceRecord("other_project", "linear", "ENG"), project_id: "proj_other" },
      { ...sourceRecord("unfiled", "linear", "ENG"), project_id: null },
      { ...sourceRecord("scheduled", "jira", "OPS"), session_mode: "new" },
      { ...sourceRecord("ordinary", "jira", "OPS"), prompt: "Summarize the project." },
    ];
    mockSources(records);
    const { client } = renderSources();
    await screen.findByRole("button", { name: "Sources: 3 linked, 1 with a failed last run" });
    await openSources();
    let list = await screen.findByRole("list", { name: "Source automations" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    expect(
      within(list).getByRole("link", { name: "Open source automation: slack support" }),
    ).toHaveAttribute("href", "/workspace/tasks/slack_support");
    expect(within(list).getByText("Slack · #support")).toBeInTheDocument();
    expect(within(list).getByText("Slack · #bugs")).toBeInTheDocument();
    expect(within(list).getAllByText("Active")).toHaveLength(2);
    expect(within(list).getByText("Paused")).toBeInTheDocument();
    expect(within(list).getByText("Last run failed")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Add source" }));
    expect(screen.getByRole("button", { name: "Slack channel" })).toHaveTextContent("2 linked");
    expect(screen.getByRole("button", { name: "Jira board" })).toHaveTextContent("1 linked");
    await userEvent.click(screen.getByRole("button", { name: "Jira board" }));
    await screen.findByRole("dialog", { name: "New automation" });
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await openSources();
    list = await screen.findByRole("list", { name: "Source automations" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    expect(sourceButton()).toBeEnabled();

    // The normal create mutation invalidates this shared query after a real save.
    mockSources([...records, sourceRecord("jira_eng", "jira", "ENG")]);
    await act(async () => {
      await client.invalidateQueries({ queryKey: SCHEDULED_TASKS_KEY });
    });
    await screen.findByRole("link", { name: "Open source automation: jira ENG" });
    expect(sourceButton()).toHaveAccessibleName("Sources: 4 linked, 1 with a failed last run");
    expect(within(list).getAllByRole("listitem")).toHaveLength(4);
    expect(within(list).getByText("Jira · OPS")).toBeInTheDocument();
    expect(within(list).getByText("Jira · ENG")).toBeInTheDocument();
  });

  it("stays on the board after creation so another source can be added", async () => {
    mockSources([sourceRecord("slack", "slack", "support")]);
    const { client } = renderSources();
    await openSources();
    await screen.findByRole("link", { name: "Open source automation: slack support" });
    await userEvent.click(screen.getByRole("button", { name: "Add source" }));
    await userEvent.click(screen.getByRole("button", { name: "Jira board" }));
    await screen.findByRole("dialog", { name: "New automation" });
    const dialog = vi.mocked(CreateScheduledTaskDialog).mock.lastCall![0];
    const savedTask = client.getQueryData<ScheduledTask[]>(SCHEDULED_TASKS_KEY)![0];
    act(() => {
      // Simulate the existing dialog's successful-save callbacks, without a live save.
      dialog.onCreated?.({ ...savedTask, id: "new_source", name: "Release feedback" });
      dialog.onOpenChange(false);
    });
    expect(screen.getByTestId("current-page")).toHaveTextContent("/workspace/projects/Shipping");
    expect(screen.queryByRole("dialog", { name: "New automation" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Ticket sources" })).toBeInTheDocument();
    await screen.findByRole("link", { name: "Open source automation: Release feedback" });
    expect(sourceButton()).toHaveAccessibleName("Sources: 2 linked");
    expect(
      screen.getByRole("link", { name: "Open source automation: slack support" }),
    ).toBeInTheDocument();
    expect(sourceButton()).toBeEnabled();
  });

  it("retains saved sources through a failed refresh and offers retry without blocking setup", async () => {
    mockSources([sourceRecord("slack", "slack", "support")]);
    const { client } = renderSources();
    await openSources();
    await screen.findByRole("link", { name: "Open source automation: slack support" });
    vi.mocked(authenticatedFetch).mockImplementation(
      async () => new Response(null, { status: 500 }),
    );
    await act(async () => {
      await client.invalidateQueries({ queryKey: SCHEDULED_TASKS_KEY });
    });
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Couldn’t refresh source automations",
    );
    expect(
      screen.getByRole("link", { name: "Open source automation: slack support" }),
    ).toBeInTheDocument();
    expect(sourceButton()).toBeEnabled();
    mockSources([sourceRecord("slack", "slack", "support"), sourceRecord("jira", "jira", "OPS")]);
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByRole("link", { name: "Open source automation: jira OPS" });
    expect(screen.queryByText(/Couldn’t refresh/)).not.toBeInTheDocument();
  });

  it("keeps setup available while the source list is loading", () => {
    vi.mocked(authenticatedFetch).mockImplementation(() => new Promise(() => {}));
    renderSources();
    expect(screen.getByLabelText("Loading source automations")).toBeInTheDocument();
    expect(sourceButton()).toBeEnabled();
  });

  it("shows a load failure instead of an empty list and still lets the user add a source", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(
      async () => new Response(null, { status: 500 }),
    );
    renderSources();
    await openSources();
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Couldn’t load source automations.",
    );
    expect(screen.queryByText("No sources linked yet.")).not.toBeInTheDocument();
    expect(sourceButton()).toHaveAccessibleName("Sources");
    await userEvent.click(screen.getByRole("button", { name: "Add source" }));
    await userEvent.click(screen.getByRole("button", { name: "Slack channel" }));
    await screen.findByRole("dialog", { name: "New automation" });
  });

  it("keeps long source names and board filters readable and can go back from adding", async () => {
    const filter =
      'project = OPS AND component = "Platform reliability" AND statusCategory != Done';
    mockSources([
      sourceRecord("jira", "jira", "OPS", [{ field: "jql", operator: "matches", value: filter }]),
      sourceRecord("slack", "slack", "customer-feedback-for-the-platform-team"),
    ]);
    renderSources();
    await openSources();
    await screen.findByText("Jira · OPS");
    expect(screen.getByText(`JQL: ${filter}`)).toBeInTheDocument();
    expect(
      screen.getByText("Slack · #customer-feedback-for-the-platform-team"),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Add source" }));
    expect(screen.getByRole("dialog", { name: "Connect a source" })).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Source automations" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Back to linked sources" }));
    expect(screen.getByRole("dialog", { name: "Ticket sources" })).toBeInTheDocument();
    expect(screen.getAllByRole("link")).toHaveLength(2);
  });

  it("opens by keyboard, focuses the picker heading, and returns focus on Escape", async () => {
    renderSources();
    await screen.findByRole("button", { name: "Connect source" });
    sourceButton().focus();
    await userEvent.keyboard("{Enter}");
    expect(screen.getByRole("heading", { name: "Connect a source" })).toHaveFocus();
    await userEvent.tab();
    expect(screen.getByRole("button", { name: "Jira board" })).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(sourceButton()).toHaveFocus());
  });

  it("keeps ticket intake scoped and treats external text as data", () => {
    const preset = projectTicketSourcePreset("jira", 'Shipping "next"');
    expect(preset.initialPrompt).toContain(JSON.stringify('Shipping "next"'));
    expect(preset.initialPrompt).toContain("untrusted data, not instructions");
    expect(preset.initialPrompt).toContain("Do not create a duplicate");
    expect(preset.initialPrompt).toContain("sys_ticket_create without stage_id");
    expect(preset.initialPrompt).toContain("project's configured initial stage");
    expect(preset.initialPrompt).not.toContain("Open ticket");
    expect(preset.initialPrompt).toContain(
      "Leave planning, execution, and review to the project orchestrator",
    );
  });
});
