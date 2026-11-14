import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { emptyTriggerFields, renderTriggerPrompt } from "@/components/scheduled/triggerTemplates";
import { showToast } from "@/components/ui/toast";
import { SCHEDULED_TASKS_KEY } from "@/hooks/useScheduledTasks";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { authenticatedFetch } from "@/lib/identity";
import type * as IdentityModule from "@/lib/identity";
import { savedWorkflowMessage } from "@/lib/workflow";
import { DeleteWorkflowDialog } from "./DeleteWorkflowDialog";

// Exercise the real query and deletion hooks against controlled API responses.
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: vi.fn(),
}));
vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));

const WORKFLOW = {
  id: "0123456789abcdef0123456789abcdef",
  name: "Triage issues",
  description: "Review new issues.",
};
const AUTOMATIONS = [
  {
    id: "daily",
    name: "Daily triage",
    prompt: savedWorkflowMessage("/", { ...WORKFLOW, name: "Old workflow name" }, "", false),
    state: "active",
  },
  {
    id: "weekend",
    name: "Weekend triage",
    prompt: renderTriggerPrompt(
      "github",
      {
        ...emptyTriggerFields("github"),
        scope: "example/project",
        instructions: savedWorkflowMessage("$", WORKFLOW, "Only urgent issues", false),
      },
      { placement: "same_host", sessionLink: "https://example.test/c/CONVERSATION_ID" },
    ),
    state: "paused",
  },
  {
    id: "unrelated",
    name: "Unrelated automation",
    prompt: savedWorkflowMessage("/", { ...WORKFLOW, id: "a".repeat(32) }, "", false),
    state: "active",
  },
];

function renderDialog({ automationsEnabled = true, client = new QueryClient() } = {}) {
  const onClose = vi.fn();
  const onDeleted = vi.fn();
  render(
    <QueryClientProvider client={client}>
      <CapabilitiesProvider
        info={{
          ...FALLBACK_SERVER_INFO,
          automations_enabled: automationsEnabled,
          features: { automations_v2: true },
        }}
      >
        <MemoryRouter>
          <DeleteWorkflowDialog workflow={WORKFLOW} onClose={onClose} onDeleted={onDeleted} />
        </MemoryRouter>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
  return { onClose, onDeleted };
}

const deleteButton = () => screen.getByRole("button", { name: "Delete workflow" });
const deletions = () =>
  vi.mocked(authenticatedFetch).mock.calls.filter(([, init]) => init?.method === "DELETE");
const automationResponse = (tasks = AUTOMATIONS) =>
  new Response(JSON.stringify({ scheduled_tasks: tasks }));

describe("DeleteWorkflowDialog", () => {
  beforeEach(() => {
    vi.mocked(showToast).mockClear();
    vi.mocked(authenticatedFetch)
      .mockReset()
      .mockImplementation(async (_path, init) =>
        init?.method === "DELETE" ? new Response(null, { status: 204 }) : automationResponse(),
      );
  });
  afterEach(cleanup);

  it("warns about active and paused automations, including trigger prompts, and lets the user cancel", async () => {
    const { onClose } = renderDialog();
    expect(deleteButton()).toBeDisabled();
    const notice = await screen.findByTestId("workflow-automation-delete-notice");
    expect(notice).toHaveTextContent("This workflow is used by 2 automations");
    expect(notice).toHaveTextContent(
      "Future runs of “Daily triage” and “Weekend triage” won’t be able to load this workflow.",
    );
    expect(screen.queryByText("Unrelated automation")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledOnce();
    expect(deletions()).toEqual([]);
  });

  it("links to each affected automation and closes the dialog when followed", async () => {
    const { onClose } = renderDialog();
    const link = await screen.findByRole("link", { name: "Weekend triage" });
    expect(link).toHaveAttribute("href", "/tasks/weekend");
    fireEvent.click(link);
    expect(onClose).toHaveBeenCalledOnce();
    expect(deletions()).toEqual([]);
  });

  it("deletes only the workflow after confirmation and keeps it on a deletion error", async () => {
    const { onClose, onDeleted } = renderDialog();
    await screen.findByTestId("workflow-automation-delete-notice");
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(new Response(null, { status: 500 }));
    fireEvent.click(deleteButton());
    await waitFor(() => expect(showToast).toHaveBeenCalledWith("Couldn’t delete “Triage issues”."));
    expect(onClose).not.toHaveBeenCalled();
    expect(onDeleted).not.toHaveBeenCalled();

    await waitFor(() => expect(deleteButton()).toBeEnabled());
    fireEvent.click(deleteButton());
    await waitFor(() => expect(onDeleted).toHaveBeenCalledOnce());
    expect(onClose).toHaveBeenCalledOnce();
    expect(deletions().map(([path]) => path)).toEqual([
      `/v1/workflows/${WORKFLOW.id}`,
      `/v1/workflows/${WORKFLOW.id}`,
    ]);
  });

  it("checks fresh usage before enabling deletion and retries a failed lookup", async () => {
    const client = new QueryClient();
    client.setQueryData(SCHEDULED_TASKS_KEY, []);
    let finish!: (response: Response) => void;
    vi.mocked(authenticatedFetch).mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          finish = resolve;
        }),
    );
    renderDialog({ client });
    expect(screen.getByRole("status")).toHaveTextContent("Checking automations…");
    expect(deleteButton()).toBeDisabled();
    fireEvent.click(deleteButton());
    expect(deletions()).toEqual([]);

    await act(async () => finish(new Response(null, { status: 500 })));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn’t check which automations use this workflow.",
    );
    expect(deleteButton()).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("workflow-automation-delete-notice")).toHaveTextContent(
      "Daily triage",
    );
    expect(deleteButton()).toBeEnabled();
  });

  it.each([true, false])(
    "confirms deletion without a usage warning when no automation uses it (automations enabled: %s)",
    async (automationsEnabled) => {
      vi.mocked(authenticatedFetch).mockImplementation(async () => automationResponse([]));
      const { onDeleted } = renderDialog({ automationsEnabled });
      await waitFor(() => expect(deleteButton()).toBeEnabled());
      expect(screen.queryByTestId("workflow-automation-delete-notice")).toBeNull();
      if (!automationsEnabled) expect(authenticatedFetch).not.toHaveBeenCalled();
      fireEvent.click(deleteButton());
      await waitFor(() => expect(onDeleted).toHaveBeenCalledOnce());
    },
  );
});
