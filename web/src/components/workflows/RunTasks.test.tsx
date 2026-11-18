import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FullscreenIcon } from "@/components/ai-elements/MermaidBlock";
import type * as SessionsApi from "@/lib/sessionsApi";

import { RunGraphButton, RunPlanButton, RunWorkflowButton, useRunTaskMenus } from "./RunTasks";

const api = vi.hoisted(() => ({ interrupt: vi.fn(), sendUserMessage: vi.fn() }));
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApi>()),
  ...api,
}));
const toastError = vi.hoisted(() => vi.fn());
vi.mock("sonner", () => ({ toast: { error: toastError } }));

function renderButton(control: "play" | "stop") {
  const queryClient = new QueryClient();
  const invalidate = vi.spyOn(queryClient, "invalidateQueries");
  render(
    <QueryClientProvider client={queryClient}>
      <RunPlanButton
        sessionId="sess_1"
        runId="plan_1"
        planName="Triage flaky tests"
        control={control}
        refreshKeys={[["workflow-runs", "wf_1"]]}
      />
    </QueryClientProvider>,
  );
  return { invalidate };
}

describe("RunPlanButton", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("asks a stopped run's session to continue the plan, then refreshes the runs", async () => {
    api.sendUserMessage.mockResolvedValue({ queued: false });
    const { invalidate } = renderButton("play");
    fireEvent.click(screen.getByRole("button", { name: "Continue the plan" }));

    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ["workflow-runs", "wf_1"] }),
    );
    expect(api.sendUserMessage).toHaveBeenCalledWith(
      "sess_1",
      expect.stringContaining('Continue the plan "Triage flaky tests" from where you left off'),
      "plan_1",
    );
    expect(api.sendUserMessage.mock.calls[0]![1]).toContain("Resume or restart");
    expect(api.sendUserMessage.mock.calls[0]![1]).toContain("Omnigent sessions");
    expect(api.sendUserMessage.mock.calls[0]![1]).toContain("recursively");
    expect(api.interrupt).not.toHaveBeenCalled();
  });

  it("asks a running run's main agent to stop all of its workers recursively", async () => {
    api.sendUserMessage.mockResolvedValue({ queued: true });
    const { invalidate } = renderButton("stop");
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));

    await waitFor(() =>
      expect(api.sendUserMessage).toHaveBeenCalledWith(
        "sess_1",
        expect.stringContaining('Stop the plan "Triage flaky tests"'),
        "plan_1",
      ),
    );
    const message = api.sendUserMessage.mock.calls[0]![1] as string;
    expect(message).toContain("background tasks, subagents, and other Omnigent sessions");
    expect(message).toContain("recursively");
    expect(message).toContain("verify");
    expect(api.interrupt).not.toHaveBeenCalled();
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["workflow-runs", "wf_1"] });
  });

  it.each(["The session is offline.", "This run is no longer current."])(
    "reports a rejected stop and refreshes the runs: %s",
    async (message) => {
      api.sendUserMessage.mockRejectedValueOnce(new Error(message));
      const { invalidate } = renderButton("stop");
      fireEvent.click(screen.getByRole("button", { name: "Stop" }));

      await waitFor(() => expect(toastError).toHaveBeenCalledWith(message));
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ["workflow-runs", "wf_1"] });
      expect(api.interrupt).not.toHaveBeenCalled();
      expect(screen.getByRole("button", { name: "Stop" })).toBeEnabled();
    },
  );

  it("reports a denied stop message without interrupting the main agent", async () => {
    api.sendUserMessage.mockResolvedValueOnce({ queued: false, denied: true });
    renderButton("stop");
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));

    await waitFor(() => expect(toastError).toHaveBeenCalledWith("A policy blocked the message."));
    expect(api.interrupt).not.toHaveBeenCalled();
  });

  it("uses the same coordinated stop from a run's task menus", async () => {
    api.sendUserMessage.mockResolvedValue({ queued: true });
    const client = new QueryClient();
    const { result } = renderHook(() => useRunTaskMenus("sess_1", [], "plan_1"), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={client}>
          <MemoryRouter>{children}</MemoryRouter>
        </QueryClientProvider>
      ),
    });
    const menu = result.current?.({
      step: { id: "review", name: "Review changes", goal: null, depends_on: [] },
      state: "running",
      status: null,
      startedAt: 1,
      native: false,
      activeForm: null,
    });
    expect(menu?.action).toBe("stop");
    await act(async () => menu?.onSelect());

    expect(api.sendUserMessage).toHaveBeenCalledWith(
      "sess_1",
      expect.stringContaining("Stop the plan"),
      "plan_1",
    );
    expect(api.sendUserMessage.mock.calls[0]![1]).toContain("recursively");
    expect(api.interrupt).not.toHaveBeenCalled();
  });
});

describe("RunWorkflowButton and RunGraphButton", () => {
  afterEach(cleanup);

  it("render the same box, placed as the row places them, with their own icons", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <RunWorkflowButton workflowId="wf_1" className="relative -my-1" />
          <RunGraphButton runName="the run" className="relative -my-1" onClick={() => {}} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const workflowLink = screen.getByTestId("run-workflow-button");
    const graphButton = screen.getByTestId("run-graph-button");
    // A link beside a button: the same classes keep their boxes and icons level.
    expect(workflowLink.tagName).toBe("A");
    expect(graphButton.tagName).toBe("BUTTON");
    expect(workflowLink.className).toBe(graphButton.className);
    expect(workflowLink).toHaveAttribute("data-size", "icon-sm");
    expect(graphButton).toHaveAttribute("data-size", "icon-sm");
    expect(workflowLink).toHaveClass("-my-1", "items-center", "justify-center");
    expect(workflowLink.querySelector("svg")).toHaveClass("lucide-workflow");
    // The graph button keeps the diagrams' fullscreen glyph, unlike the workflow's boxes.
    const { container: fullscreenGlyph } = render(<FullscreenIcon />);
    expect(graphButton.innerHTML).toBe(fullscreenGlyph.innerHTML);
  });
});
