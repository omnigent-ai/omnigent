import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type * as SessionsApi from "@/lib/sessionsApi";

import { RunPlanButton } from "./RunTasks";

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
      'Continue the plan "Triage flaky tests" from where you left off, and work through the tasks that are left.',
    );
    expect(api.interrupt).not.toHaveBeenCalled();
  });

  it("interrupts a running run's session, and says why when it can't", async () => {
    api.interrupt.mockRejectedValueOnce(new Error("The session is offline."));
    renderButton("stop");
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));

    await waitFor(() => expect(toastError).toHaveBeenCalledWith("The session is offline."));
    expect(api.interrupt).toHaveBeenCalledWith("sess_1");
    expect(api.sendUserMessage).not.toHaveBeenCalled();
  });
});
