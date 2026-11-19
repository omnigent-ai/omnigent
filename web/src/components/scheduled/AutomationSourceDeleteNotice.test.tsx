import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import type * as ScheduledTasksApiModule from "@/lib/scheduledTasksApi";
import { getScheduledTask, type ScheduledTask } from "@/lib/scheduledTasksApi";
import { AutomationSourcesDeleteNotice } from "./AutomationSourceDeleteNotice";

vi.mock("@/lib/scheduledTasksApi", async (importOriginal) => ({
  ...(await importOriginal<typeof ScheduledTasksApiModule>()),
  getScheduledTask: vi.fn(),
}));

const NAMES: Record<string, string> = { st_1: "Weekly rubric", st_2: "Nightly triage" };

function renderNotice(taskIds: string[], onNavigate = () => {}) {
  vi.mocked(getScheduledTask).mockImplementation(
    async (id: string) => ({ id, name: NAMES[id] }) as ScheduledTask,
  );
  render(
    <QueryClientProvider client={new QueryClient()}>
      <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, features: { automations_v2: true } }}>
        <MemoryRouter>
          <AutomationSourcesDeleteNotice taskIds={taskIds} onNavigate={onNavigate} />
        </MemoryRouter>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
}

describe("AutomationSourcesDeleteNotice", () => {
  it.each([
    [
      ["st_1"],
      "1 of these sessions runs an automationDeleting it also deletes the automation “Weekly rubric”.",
    ],
    [
      ["st_1", "st_2"],
      "2 of these sessions run automationsDeleting them also deletes the automations “Weekly rubric” and “Nightly triage”.",
    ],
  ])("names the automations that %j run", async (taskIds, text) => {
    renderNotice(taskIds);

    await waitFor(() =>
      expect(screen.getByTestId("bulk-automation-source-delete-notice")).toHaveTextContent(text),
    );
  });

  it("links each automation to its page and closes the dialog when followed", async () => {
    const onNavigate = vi.fn();
    renderNotice(["st_1", "st_2"], onNavigate);

    const link = await screen.findByRole("link", { name: "Nightly triage" });
    expect(link).toHaveAttribute("href", "/tasks/st_2");
    fireEvent.click(link);
    expect(onNavigate).toHaveBeenCalledTimes(1);
  });
});
