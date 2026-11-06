import { StrictMode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { SERVER_INFO_OFFLINE_FALLBACK } from "@/lib/bootCapabilities";
import { testAgent } from "@/test/agentFixtures";
import * as api from "@/lib/scheduledTasksApi";
import { SharedAutomations } from "./SharedAutomations";

vi.mock("@/lib/scheduledTasksApi", async (original) => ({
  ...(await original<typeof api>()),
  listSharedScheduledTasks: vi.fn(),
  getSharedScheduledTask: vi.fn(),
  createScheduledTask: vi.fn(),
}));
vi.mock("@/hooks/useAvailableAgents", () => ({
  USER_AGENTS_QUERY_KEY: ["available-agents-user"],
  useAvailableAgents: () => ({
    data: [testAgent("agent", "claude-native-ui", { harness: "claude-native" })],
  }),
}));
vi.mock("@/hooks/useHosts", () => ({ useHosts: () => ({ data: [] }) }));
vi.mock("@/hooks/useConversations", () => ({ useProjects: () => ({ data: [] }) }));
vi.mock("./useScheduledHarnessSettings", () => ({
  useScheduledHarnessSettings: () => ({
    loading: false,
    supportsModel: true,
    models: [],
    efforts: [],
    permissions: [],
    blockedReason: null,
  }),
}));
vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));
vi.mock("./CreateScheduledTaskDialog", () => ({
  CreateScheduledTaskDialog: ({
    initialTask,
    copyIssues,
    onOpenChange,
  }: {
    initialTask: api.ScheduledTask;
    copyIssues: string[];
    onOpenChange: (open: boolean) => void;
  }) => (
    <div role="dialog" aria-label="Copy automation">
      <p>{initialTask.name}</p>
      {copyIssues.map((issue) => (
        <p key={issue}>{issue}</p>
      ))}
      <button type="button" onClick={() => onOpenChange(false)}>
        Cancel
      </button>
    </div>
  ),
}));

const source: api.ScheduledTask = {
  id: "shared",
  name: "Triage",
  prompt: "Triage issues",
  rrule: "FREQ=DAILY",
  timezone: "UTC",
  ownerUserId: "alice",
  agentId: "agent",
  createdAt: 0,
  updatedAt: 0,
  modelOverride: null,
  reasoningEffort: null,
  permissionMode: null,
  sessionMode: "continue",
  sessionId: null,
  workspace: null,
  hostId: null,
  executionTarget: "managed_sandbox",
  projectId: null,
  state: "active",
  lastRunAt: null,
  lastRunStatus: null,
  lastRunConversationId: null,
  nextRunAt: null,
};
beforeEach(() => {
  vi.mocked(api.listSharedScheduledTasks).mockResolvedValue([source]);
  vi.mocked(api.getSharedScheduledTask).mockResolvedValue(source);
  vi.mocked(api.createScheduledTask).mockResolvedValue({
    ...source,
    id: "copy",
    ownerUserId: "bob",
  });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function showShared(onCopied = vi.fn()) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <StrictMode>
      <QueryClientProvider client={client}>
        <CapabilitiesProvider
          info={{
            ...SERVER_INFO_OFFLINE_FALLBACK,
            managed_sandboxes_enabled: true,
            scheduled_task_sharing: true,
          }}
        >
          <SharedAutomations sharedTaskId={null} onCopied={onCopied} />
        </CapabilitiesProvider>
      </QueryClientProvider>
    </StrictMode>,
  );
  return onCopied;
}

it("shows read-only definitions and creates one compatible copy immediately", async () => {
  const onCopied = showShared();
  fireEvent.click(await screen.findByRole("button", { name: "Copy" }));
  await waitFor(() => expect(onCopied).toHaveBeenCalledTimes(1));
  expect(api.getSharedScheduledTask).toHaveBeenCalledWith("shared");
  expect(api.createScheduledTask).toHaveBeenCalledTimes(1);
  expect(api.createScheduledTask).toHaveBeenCalledWith(
    expect.objectContaining({
      prompt: "Triage issues",
      sessionMode: "continue",
      executionTarget: "managed_sandbox",
    }),
  );
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  for (const name of ["Run now", "Edit", "Delete", "Pause"])
    expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
});

it("opens configuration for an unavailable host without creating a task", async () => {
  vi.mocked(api.getSharedScheduledTask).mockResolvedValue({
    ...source,
    executionTarget: "connected_host",
    hostId: "alice-host",
    workspace: "/alice/repo",
  });
  showShared();
  fireEvent.click(await screen.findByRole("button", { name: "Copy" }));
  expect(await screen.findByRole("dialog", { name: "Copy automation" })).toBeInTheDocument();
  expect(screen.getByText(/Run on: choose your host/)).toBeInTheDocument();
  expect(api.createScheduledTask).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("opens configuration when create-time validation rejects otherwise compatible settings", async () => {
  vi.mocked(api.createScheduledTask).mockRejectedValueOnce(
    new api.ScheduledTaskApiError("Choose an available repository", 400, "invalid_input"),
  );
  showShared();
  fireEvent.click(await screen.findByRole("button", { name: "Copy" }));
  expect(await screen.findByRole("dialog", { name: "Copy automation" })).toBeInTheDocument();
  expect(screen.getByText("Choose an available repository")).toBeInTheDocument();
  expect(api.createScheduledTask).toHaveBeenCalledTimes(1);
});

it("does not copy a stale list entry after access is revoked", async () => {
  vi.mocked(api.getSharedScheduledTask).mockRejectedValueOnce(
    new api.ScheduledTaskApiError("Shared automation not found", 404, "not_found"),
  );
  showShared();
  fireEvent.click(await screen.findByRole("button", { name: "Copy" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Shared automation not found");
  expect(api.createScheduledTask).not.toHaveBeenCalled();
});

it("copies the shared custom agent even when it is absent from the recipient's catalog", async () => {
  vi.mocked(api.getSharedScheduledTask).mockResolvedValue({
    ...source,
    agentId: "private-agent",
    sharedAgent: testAgent("private-agent", "Research brief", {
      harness: "claude-native",
      shared: true,
    }),
  });
  const onCopied = showShared();
  fireEvent.click(await screen.findByRole("button", { name: "Copy" }));
  await waitFor(() => expect(onCopied).toHaveBeenCalledTimes(1));
  expect(api.createScheduledTask).toHaveBeenCalledWith(
    expect.objectContaining({
      sharedTaskId: "shared",
      agentId: "private-agent",
    }),
  );
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("preserves the custom agent while asking for the recipient's host, and does nothing on cancel", async () => {
  vi.mocked(api.getSharedScheduledTask).mockResolvedValue({
    ...source,
    agentId: "private-agent",
    executionTarget: "connected_host",
    hostId: "alice-host",
    sharedAgent: testAgent("private-agent", "Research brief", {
      harness: "claude-native",
      shared: true,
    }),
  });
  showShared();
  fireEvent.click(await screen.findByRole("button", { name: "Copy" }));
  expect(await screen.findByRole("dialog")).toBeInTheDocument();
  expect(screen.getByText(/Run on: choose your host/)).toBeInTheDocument();
  expect(screen.queryByText(/Harness: choose one available/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(api.createScheduledTask).not.toHaveBeenCalled();
});
