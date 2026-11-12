import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useHostFilesystem } from "@/hooks/useHostFilesystem";
import { type Host, useHostModelOptions, useHosts } from "@/hooks/useHosts";
import type { SavedWorkflow } from "@/hooks/useSavedWorkflows";
import { nativeWrapperLabelsForAgent } from "@/lib/nativeCodingAgents";
import type * as RoutingModule from "@/lib/routing";
import { createSession } from "@/lib/sessionsApi";
import type * as SessionsApiModule from "@/lib/sessionsApi";
import { runSavedWorkflowPrompt } from "@/lib/workflow";
import { consumePendingInitialPrompt } from "@/store/chatStore";
import { testAgent } from "@/test/agentFixtures";

import { RunWorkflowDialog } from "./RunWorkflowDialog";

vi.mock("@/hooks/useHosts", () => ({ useHosts: vi.fn(), useHostModelOptions: vi.fn() }));
vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
vi.mock("@/hooks/useHostFilesystem", () => ({ useHostFilesystem: vi.fn() }));
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

function renderDialog() {
  const onOpenChange = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <MemoryRouter>
          <RunWorkflowDialog workflow={WORKFLOW} open onOpenChange={onOpenChange} />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return { onOpenChange };
}

const submit = () => screen.getByTestId("run-workflow-submit");

describe("RunWorkflowDialog", () => {
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

    expect(screen.getByTestId("run-workflow-host")).toHaveTextContent("laptop");
    expect(screen.getByPlaceholderText("Type a path or browse…")).toHaveValue("/home/you/app");
    expect(screen.getByTestId("run-workflow-agent")).toHaveTextContent("Codex");
    const instructions = screen.getByTestId("run-workflow-instructions");
    expect(instructions).toHaveValue(runSavedWorkflowPrompt(WORKFLOW));

    fireEvent.change(instructions, { target: { value: "Run it on the flaky suite only." } });
    fireEvent.click(submit());

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/c/conv_run"));
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

    expect(await screen.findByTestId("run-workflow-error")).toHaveTextContent("Host is busy");
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
});
