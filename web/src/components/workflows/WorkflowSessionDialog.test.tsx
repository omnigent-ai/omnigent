import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useHostFilesystem } from "@/hooks/useHostFilesystem";
import { type Host, useHostModelOptions, useHosts } from "@/hooks/useHosts";
import { useSandboxModelOptions } from "@/hooks/useSandboxModelOptions";
import type * as SandboxModelOptionsModule from "@/hooks/useSandboxModelOptions";
import type { SavedWorkflow } from "@/hooks/useSavedWorkflows";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import { nativeWrapperLabelsForAgent } from "@/lib/nativeCodingAgents";
import type * as RoutingModule from "@/lib/routing";
import { createSession } from "@/lib/sessionsApi";
import type * as SessionsApiModule from "@/lib/sessionsApi";
import { REVIEW_PLAN_FIRST, createWorkflowPrompt, runSavedWorkflowPrompt } from "@/lib/workflow";
import { consumePendingInitialPrompt } from "@/store/chatStore";
import { testAgent } from "@/test/agentFixtures";

import { type WorkflowSessionRequest, WorkflowSessionDialog } from "./WorkflowSessionDialog";

vi.mock("@/hooks/useHosts", () => ({ useHosts: vi.fn(), useHostModelOptions: vi.fn() }));
vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
vi.mock("@/hooks/useHostFilesystem", () => ({ useHostFilesystem: vi.fn() }));
vi.mock("@/hooks/useSandboxModelOptions", async (importOriginal) => ({
  ...(await importOriginal<typeof SandboxModelOptionsModule>()),
  useSandboxModelOptions: vi.fn(),
}));
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  createSession: vi.fn(),
}));
const navigate = vi.fn();
vi.mock("@/lib/routing", async (importOriginal) => {
  const actual = await importOriginal<typeof RoutingModule>();
  return { ...actual, useNavigate: () => navigate };
});

const WORKFLOW: SavedWorkflow = {
  id: "6f1c2b9a4d8e4f30a1b2c3d4e5f60718",
  name: "Fix flaky tests",
  description: "Reruns the suite and files an issue for each flaky test.",
};
const CLAUDE_CODE = testAgent("ag_claude_native", "claude-native-ui", {
  display_name: "Claude Code",
  harness: "claude-native",
});
const CODEX = testAgent("ag_codex_native", "codex-native-ui", {
  display_name: "Codex",
  harness: "codex-native",
});

function setHosts(hosts: Partial<Host>[]) {
  vi.mocked(useHosts).mockReturnValue({
    data: hosts.map((host) => ({ name: "laptop", owner: "you", status: "online", ...host })),
  } as ReturnType<typeof useHosts>);
}

// A server that can launch Modal and Daytona sandboxes.
const SANDBOXES: Partial<ServerInfo> = {
  managed_sandboxes_enabled: true,
  sandbox_provider: "modal",
  sandbox_providers: ["modal", "daytona"],
};

const RUN: WorkflowSessionRequest = { kind: "run", workflow: WORKFLOW };
const CREATE: WorkflowSessionRequest = {
  kind: "create",
  name: "Upgrade a dependency",
  description: "Bump one package and fix what breaks",
};

function renderDialog(info: Partial<ServerInfo> = {}, request: WorkflowSessionRequest = RUN) {
  const onOpenChange = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, ...info }}>
        <TooltipProvider>
          <MemoryRouter>
            <WorkflowSessionDialog request={request} open onOpenChange={onOpenChange} />
          </MemoryRouter>
        </TooltipProvider>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
  return { onOpenChange };
}

function setSandboxModels(data: Partial<SandboxModelOptionsModule.SandboxModelOptions>) {
  vi.mocked(useSandboxModelOptions).mockReturnValue({
    data: { models: [], configured: true, status: "ready", ...data },
    isLoading: false,
    isError: false,
  } as unknown as ReturnType<typeof useSandboxModelOptions>);
}

const submit = () => screen.getByTestId("workflow-session-submit");
const startedSession = () =>
  vi
    .mocked(createSession)
    .mockResolvedValue({ id: "conv_run" } as Awaited<ReturnType<typeof createSession>>);

describe("WorkflowSessionDialog", () => {
  beforeEach(() => {
    localStorage.clear();
    navigate.mockClear();
    vi.mocked(createSession).mockReset();
    vi.mocked(useAvailableAgents).mockReturnValue({
      data: [CLAUDE_CODE, CODEX],
    } as unknown as ReturnType<typeof useAvailableAgents>);
    vi.mocked(useHostModelOptions).mockReturnValue({
      data: [],
      isLoading: false,
    } as unknown as ReturnType<typeof useHostModelOptions>);
    vi.mocked(useHostFilesystem).mockReturnValue({
      data: undefined,
      isLoading: false,
    } as unknown as ReturnType<typeof useHostFilesystem>);
    vi.mocked(useSandboxModelOptions).mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: false,
    } as unknown as ReturnType<typeof useSandboxModelOptions>);
  });
  afterEach(cleanup);

  it("starts a session where the new-session page last ran, with the run request first", async () => {
    setHosts([
      { host_id: "host_1", name: "desk" },
      { host_id: "host_2", name: "laptop" },
    ]);
    localStorage.setItem("omnigent:last-host-choice", "host_2");
    localStorage.setItem(
      "omnigent:recent-workspaces",
      JSON.stringify({ host_2: ["/home/you/app"] }),
    );
    localStorage.setItem("omnigent:last-agent-id", CODEX.id);
    vi.mocked(createSession).mockResolvedValue({ id: "conv_run" } as Awaited<
      ReturnType<typeof createSession>
    >);
    const { onOpenChange } = renderDialog();

    expect(screen.getByTestId("workflow-session-host")).toHaveTextContent("laptop");
    expect(screen.getByPlaceholderText("Type a path or browse…")).toHaveValue("/home/you/app");
    expect(screen.getByTestId("workflow-session-agent")).toHaveTextContent("Codex");
    const instructions = screen.getByTestId("workflow-session-instructions");
    expect(instructions).toHaveValue(runSavedWorkflowPrompt(WORKFLOW));

    fireEvent.change(instructions, { target: { value: "Run it on the flaky suite only." } });
    fireEvent.click(submit());

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/c/conv_run"));
    expect(vi.mocked(createSession).mock.calls[0]![2]).not.toHaveProperty(
      "title",
      expect.anything(),
    );
    expect(createSession).toHaveBeenCalledWith(CODEX.id, [], {
      hostId: "host_2",
      workspace: "/home/you/app",
      labels: nativeWrapperLabelsForAgent(CODEX),
      terminalLaunchArgs: undefined,
      modelOverride: undefined,
      reasoningEffort: undefined,
    });
    expect(consumePendingInitialPrompt("conv_run")).toEqual({
      text: "Run it on the flaky suite only.",
      skill: null,
      files: [],
    });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("keeps the dialog open with the server's reason when the session can't start", async () => {
    setHosts([{ host_id: "host_1" }]);
    localStorage.setItem("omnigent:recent-workspaces", JSON.stringify({ host_1: ["/srv/app"] }));
    vi.mocked(createSession).mockRejectedValue(new Error("Host is busy"));
    const { onOpenChange } = renderDialog();

    fireEvent.click(submit());

    expect(await screen.findByTestId("workflow-session-error")).toHaveTextContent("Host is busy");
    expect(navigate).not.toHaveBeenCalled();
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(submit()).toBeEnabled();
  });

  it("needs an absolute directory before it runs", () => {
    setHosts([{ host_id: "host_1" }]);
    renderDialog();

    const directory = screen.getByPlaceholderText("Type a path or browse…");
    expect(directory).toHaveValue("");
    expect(submit()).toBeDisabled();

    fireEvent.change(directory, { target: { value: "projects/app" } });
    expect(screen.getByText(/Enter an absolute path/)).toBeInTheDocument();
    expect(submit()).toBeDisabled();

    fireEvent.change(directory, { target: { value: "/srv/app" } });
    expect(screen.queryByText(/Enter an absolute path/)).toBeNull();
    expect(submit()).toBeEnabled();
  });

  it("shows how to connect a host when none is online", () => {
    setHosts([{ host_id: "host_1", status: "offline" }]);
    renderDialog();

    expect(
      screen.getByText("No hosts online. Reconnect one from your terminal:"),
    ).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Type a path or browse…")).toBeNull();
    expect(submit()).toBeDisabled();
  });

  it("runs in the sandbox the new-session page last used, cloning its last repositories", async () => {
    setHosts([{ host_id: "host_1" }]);
    localStorage.setItem("omnigent:last-host-choice", "__sandbox__");
    localStorage.setItem("omnigent:last-sandbox-provider", "daytona");
    localStorage.setItem(
      "omnigent:last-sandbox-repos",
      JSON.stringify([{ url: "https://github.com/example/app", branch: "main" }]),
    );
    localStorage.setItem("omnigent:last-agent-id", CLAUDE_CODE.id);
    startedSession();
    renderDialog({
      ...SANDBOXES,
      durable_initial_input: true,
      request_managed_sandbox_host_id: true,
    });

    expect(screen.getByTestId("workflow-session-host")).toHaveTextContent("Daytona Sandbox");
    expect(screen.queryByPlaceholderText("Type a path or browse…")).toBeNull();
    expect(screen.getByTestId("workflow-session-sandbox-repo-row")).toHaveTextContent("app");
    fireEvent.click(submit());

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/c/conv_run"));
    expect(createSession).toHaveBeenCalledWith(CLAUDE_CODE.id, [], {
      sandbox: {
        provider: "daytona",
        workspaces: ["https://github.com/example/app#main"],
        hostId: expect.stringMatching(/^[0-9a-f]{32}$/),
      },
      // This server sends the first message itself once the sandbox is up.
      initialInput: {
        type: "message",
        data: {
          role: "user",
          content: [{ type: "input_text", text: runSavedWorkflowPrompt(WORKFLOW) }],
        },
      },
      labels: nativeWrapperLabelsForAgent(CLAUDE_CODE),
    });
    expect(consumePendingInitialPrompt("conv_run")).toBeNull();
  });

  it("starts on a sandbox when no host is online, sending the first message from the page", async () => {
    setHosts([{ host_id: "host_1", status: "offline" }]);
    startedSession();
    renderDialog(SANDBOXES);

    expect(screen.getByTestId("workflow-session-host")).toHaveTextContent("Modal Sandbox");
    expect(screen.queryByText(/Reconnect one from your terminal/)).toBeNull();
    expect(screen.getByText(/starts in an empty workspace/)).toBeInTheDocument();
    fireEvent.click(submit());

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/c/conv_run"));
    expect(vi.mocked(createSession).mock.calls[0]![2]).toMatchObject({
      sandbox: { provider: "modal", workspaces: [] },
      initialInput: undefined,
    });
    expect(consumePendingInitialPrompt("conv_run")).toEqual({
      text: runSavedWorkflowPrompt(WORKFLOW),
      skill: null,
      files: [],
    });
  });

  it("waits for a typed repository URL to be added or cleared", () => {
    setHosts([]);
    renderDialog(SANDBOXES);

    fireEvent.change(screen.getByTestId("workflow-session-sandbox-repo-input"), {
      target: { value: "https://github.com/example/app" },
    });
    expect(screen.getByText("Add or clear the repository URL to run.")).toBeInTheDocument();
    expect(submit()).toBeDisabled();

    fireEvent.click(screen.getByTestId("workflow-session-sandbox-repo-add"));
    expect(screen.getByTestId("workflow-session-sandbox-repo-row")).toHaveTextContent("app");
    expect(submit()).toBeEnabled();
  });

  it("lists the sandbox's models, and blocks an agent it can't run", () => {
    setHosts([]);
    localStorage.setItem("omnigent:last-agent-id", CLAUDE_CODE.id);
    const catalog = { sandbox_provider_capabilities: { modal: { default_models: true } } };
    setSandboxModels({ models: [{ id: "sonnet", displayName: "Sonnet 4.5", isDefault: true }] });
    renderDialog({ ...SANDBOXES, ...catalog });

    expect(screen.getByTestId("fork-session-config-model")).toHaveTextContent("Sonnet 4.5");
    expect(submit()).toBeEnabled();

    cleanup();
    setSandboxModels({ status: "unsupported", error: "No sandbox model can run Claude Code." });
    renderDialog({ ...SANDBOXES, ...catalog });

    expect(screen.getByText("No sandbox model can run Claude Code.")).toBeInTheDocument();
    expect(submit()).toBeDisabled();
  });

  it("asks for a plan review only when its box is checked", async () => {
    setHosts([{ host_id: "host_1" }]);
    localStorage.setItem("omnigent:recent-workspaces", JSON.stringify({ host_1: ["/srv/app"] }));
    startedSession();
    renderDialog();

    expect(screen.getByTestId("workflow-session-review-plan")).not.toBeChecked();
    fireEvent.click(submit());
    await waitFor(() => expect(navigate).toHaveBeenCalled());
    expect(consumePendingInitialPrompt("conv_run")?.text).toBe(runSavedWorkflowPrompt(WORKFLOW));

    cleanup();
    renderDialog();
    fireEvent.click(screen.getByTestId("workflow-session-review-plan"));
    fireEvent.click(submit());
    await waitFor(() => expect(navigate).toHaveBeenCalledTimes(2));
    expect(consumePendingInitialPrompt("conv_run")?.text).toBe(
      `${runSavedWorkflowPrompt(WORKFLOW)}\n\n${REVIEW_PLAN_FIRST}`,
    );
  });

  it("starts the session that drafts a new workflow, from New workflow", async () => {
    setHosts([{ host_id: "host_1" }]);
    localStorage.setItem("omnigent:recent-workspaces", JSON.stringify({ host_1: ["/srv/app"] }));
    startedSession();
    renderDialog({}, CREATE);

    expect(screen.getByRole("dialog", { name: "New workflow" })).toBeInTheDocument();
    expect(screen.getByTestId("workflow-session-instructions")).toHaveValue(
      createWorkflowPrompt("Upgrade a dependency", "Bump one package and fix what breaks"),
    );
    // Drafting has no plan to review.
    expect(screen.queryByTestId("workflow-session-review-plan")).toBeNull();
    expect(submit()).toHaveTextContent("Create");
    fireEvent.click(submit());

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/c/conv_run"));
    expect(vi.mocked(createSession).mock.calls[0]![2]).toMatchObject({
      title: "Create workflow Upgrade a dependency",
      hostId: "host_1",
      workspace: "/srv/app",
    });
    expect(consumePendingInitialPrompt("conv_run")?.text).toBe(
      createWorkflowPrompt("Upgrade a dependency", "Bump one package and fix what breaks"),
    );
  });

  it("still asks where New workflow runs with no host online", () => {
    setHosts([]);
    renderDialog({}, CREATE);

    expect(screen.getByRole("dialog", { name: "New workflow" })).toBeInTheDocument();
    expect(
      screen.getByText("No hosts connected yet. Connect one from your terminal:"),
    ).toBeInTheDocument();
    expect(submit()).toBeDisabled();
  });
});
