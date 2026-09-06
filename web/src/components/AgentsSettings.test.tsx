import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { CustomAgent, CustomAgentDetail } from "@/lib/customAgentsApi";
import { AgentsSettings } from "./AgentsSettings";

const mocks = vi.hoisted(() => ({
  available: [] as AvailableAgent[],
  catalog: [] as CustomAgent[],
  refetch: vi.fn(),
  createCustomAgent: vi.fn(),
  deleteCustomAgent: vi.fn(),
  getCustomAgent: vi.fn(),
  importCustomAgent: vi.fn(),
  updateCustomAgent: vi.fn(),
  buildAgentBundle: vi.fn(),
}));

vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({ data: mocks.available, isLoading: false, error: null }),
}));

vi.mock("@/lib/customAgentsApi", () => ({
  CUSTOM_AGENTS_QUERY_KEY: ["custom-agents"],
  useCustomAgents: () => ({
    data: mocks.catalog,
    isLoading: false,
    error: null,
    refetch: mocks.refetch,
  }),
  createCustomAgent: mocks.createCustomAgent,
  deleteCustomAgent: mocks.deleteCustomAgent,
  getCustomAgent: mocks.getCustomAgent,
  importCustomAgent: mocks.importCustomAgent,
  updateCustomAgent: mocks.updateCustomAgent,
}));

vi.mock("@/lib/agentBundle", () => ({ buildAgentBundle: mocks.buildAgentBundle }));
vi.mock("@/lib/agentLabels", () => ({
  BRAIN_HARNESS_LABELS: { codex: "Codex" },
  useBrainHarnessLabels: () => ({ codex: "Codex" }),
}));
vi.mock("@/lib/analytics", () => ({
  useOmnigentAnalytics: () => ({ trackValueChange: vi.fn() }),
}));
vi.mock("@/hooks/useSuppressBrowserView", () => ({ SuppressBrowserView: () => null }));

const custom: CustomAgent = {
  id: "ca_custom_reviewer",
  name: "Reviewer",
  description: "Reviews changes",
  harness: "codex",
  model: null,
  version: 3,
  created_at: 2,
  updated_at: null,
};

const customDetail: CustomAgentDetail = {
  ...custom,
  instructions: "Review carefully.",
};

const importable: AvailableAgent = {
  id: "ag_session_writer",
  name: "writer",
  display_name: "Writer",
  description: "Writes release notes",
  harness: "codex",
  skills: [],
  sessionId: "session_writer",
};

function renderSettings() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  return render(
    <QueryClientProvider client={client}>
      <AgentsSettings />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mocks.available = [
    importable,
    { ...importable, id: "ag_catalog_clone", sessionId: "session_clone", templateId: custom.id },
    {
      ...importable,
      id: "ag_orphaned_clone",
      display_name: "Orphaned clone",
      sessionId: "session_orphaned",
      templateId: "ca_deleted",
    },
  ];
  mocks.catalog = [custom];
  mocks.getCustomAgent.mockResolvedValue(customDetail);
  mocks.updateCustomAgent.mockResolvedValue(customDetail);
  mocks.deleteCustomAgent.mockResolvedValue(undefined);
  mocks.createCustomAgent.mockResolvedValue(customDetail);
  mocks.importCustomAgent.mockResolvedValue(customDetail);
  mocks.buildAgentBundle.mockResolvedValue(
    new File(["bundle"], "agent.tar.gz", { type: "application/gzip" }),
  );
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("AgentsSettings", () => {
  it("offers new and orphaned session Agents for import", () => {
    renderSettings();

    expect(screen.getByText("Reviewer")).toBeInTheDocument();
    expect(screen.getByText("Writer")).toBeInTheDocument();
    expect(screen.getByText("Orphaned clone")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Import" })).toHaveLength(2);
  });

  it("awaits edit, delete, and import mutations before closing or refreshing", async () => {
    renderSettings();

    fireEvent.click(screen.getByRole("button", { name: "Edit Reviewer" }));
    const editor = await screen.findByRole("dialog");
    const name = await within(editor).findByRole("textbox", { name: "Name" });
    fireEvent.change(name, { target: { value: "Release reviewer" } });
    fireEvent.click(within(editor).getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(mocks.updateCustomAgent).toHaveBeenCalledWith(custom.id, {
        name: "Release reviewer",
        description: custom.description,
        instructions: customDetail.instructions,
        version: custom.version,
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Delete Reviewer" }));
    const deleteDialog = await screen.findByRole("dialog");
    fireEvent.click(within(deleteDialog).getByRole("button", { name: "Delete Agent" }));
    await waitFor(() => expect(mocks.deleteCustomAgent).toHaveBeenCalledWith(custom.id));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    const writerRow = screen.getByText("Writer").parentElement;
    expect(writerRow).not.toBeNull();
    fireEvent.click(within(writerRow!).getByRole("button", { name: "Import" }));
    await waitFor(() => expect(mocks.importCustomAgent).toHaveBeenCalledWith("session_writer"));
  });

  it("keeps create open on an API error and closes only after a successful retry", async () => {
    mocks.createCustomAgent
      .mockRejectedValueOnce(new Error("Agent could not be saved"))
      .mockResolvedValueOnce(customDetail);
    renderSettings();

    fireEvent.click(screen.getByRole("button", { name: "New Agent" }));
    const dialog = await screen.findByTestId("create-agent-dialog");
    fireEvent.change(within(dialog).getByTestId("create-agent-name"), {
      target: { value: "Reviewer" },
    });
    fireEvent.change(within(dialog).getByTestId("create-agent-model"), {
      target: { value: "gpt-default" },
    });
    fireEvent.click(within(dialog).getByTestId("create-agent-submit"));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Agent could not be saved");
    expect(screen.getByTestId("create-agent-dialog")).toBeInTheDocument();

    fireEvent.click(within(dialog).getByTestId("create-agent-submit"));
    await waitFor(() => expect(mocks.createCustomAgent).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.queryByTestId("create-agent-dialog")).not.toBeInTheDocument(),
    );
  });
});
