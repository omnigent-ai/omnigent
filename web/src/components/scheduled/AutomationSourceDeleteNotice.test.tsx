import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import type * as ScheduledTasksApiModule from "@/lib/scheduledTasksApi";
import { getScheduledTask, type ScheduledTask } from "@/lib/scheduledTasksApi";
import { AutomationSourcesDeleteNotice } from "./AutomationSourceDeleteNotice";

vi.mock("@/lib/scheduledTasksApi", async (importOriginal) => ({
  ...(await importOriginal<typeof ScheduledTasksApiModule>()),
  getScheduledTask: vi.fn(),
}));

const NAMES: Record<string, string> = { st_1: "Weekly rubric", st_2: "Nightly triage" };

describe("AutomationSourcesDeleteNotice", () => {
  it.each([
    [["st_1"], "1 of these sessions runs an automationDeleting it also deletes “Weekly rubric”."],
    [
      ["st_1", "st_2"],
      "2 of these sessions run automationsDeleting them also deletes “Weekly rubric” and “Nightly triage”.",
    ],
  ])("names the automations that %j run", async (taskIds, text) => {
    vi.mocked(getScheduledTask).mockImplementation(
      async (id: string) => ({ id, name: NAMES[id] }) as ScheduledTask,
    );
    render(
      <QueryClientProvider client={new QueryClient()}>
        <AutomationSourcesDeleteNotice taskIds={taskIds} />
      </QueryClientProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("bulk-automation-source-delete-notice")).toHaveTextContent(text),
    );
  });
});
