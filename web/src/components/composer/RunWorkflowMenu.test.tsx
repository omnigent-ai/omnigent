import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { toast } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SavedWorkflow } from "@/hooks/useSavedWorkflows";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { authenticatedFetch } from "@/lib/identity";

import { RunWorkflowMenu } from "./RunWorkflowMenu";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: vi.fn() }));

const triage: SavedWorkflow = {
  id: "a".repeat(32),
  name: "Triage flaky tests",
  description: "Finds and fixes flaky tests.",
};
const draft: SavedWorkflow = {
  id: "b".repeat(32),
  name: "Unfinished workflow",
  description: "Still planning.",
  is_draft: true,
};

/**
 * Answers each request for the user's workflows with the next list, or a server
 * error for `"error"`, then repeats the last answer.
 */
function listWorkflows(...answers: (SavedWorkflow[] | "error")[]): () => number {
  let requests = 0;
  vi.mocked(authenticatedFetch).mockImplementation(async () => {
    const answer = answers[Math.min(requests, answers.length - 1)];
    requests += 1;
    return answer === "error"
      ? new Response(null, { status: 503, statusText: "Service Unavailable" })
      : Response.json({ data: answer, has_more: false, last_id: null });
  });
  return () => requests;
}

function renderMenu() {
  const onStart = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <RunWorkflowMenu onStart={onStart}>run a workflow</RunWorkflowMenu>
    </QueryClientProvider>,
  );
  return { onStart, link: screen.getByRole("button", { name: "run a workflow" }) };
}

beforeEach(() => {
  vi.mocked(authenticatedFetch).mockReset();
  vi.mocked(useServerInfo).mockReturnValue({
    ...FALLBACK_SERVER_INFO,
    workflows: true,
    features: { workflows: true },
  });
});

afterEach(cleanup);

describe("RunWorkflowMenu", () => {
  it("reloads an empty list before going straight to describing a workflow", async () => {
    const requests = listWorkflows([], [triage]);
    const { onStart, link } = renderMenu();
    await waitFor(() => expect(requests()).toBe(1));
    expect(link).toHaveAttribute("aria-haspopup", "dialog");

    // Saved elsewhere since the list loaded: using the link finds it.
    fireEvent.pointerDown(link, { button: 0 });
    expect(await screen.findByTestId("composer-saved-workflow")).toHaveTextContent(
      "Triage flaky tests",
    );
    expect(requests()).toBe(2);
    expect(onStart).not.toHaveBeenCalled();
  });

  it("describes a new workflow when the reloaded list has none that can run", async () => {
    const requests = listWorkflows([draft]);
    const { onStart, link } = renderMenu();
    await waitFor(() => expect(requests()).toBe(1));
    expect(link).toHaveAttribute("aria-haspopup", "dialog");

    fireEvent.pointerDown(link, { button: 0 });
    await waitFor(() => expect(onStart).toHaveBeenCalledWith());
    expect(requests()).toBe(2);
    expect(screen.queryByTestId("composer-workflow-picker")).toBeNull();
  });

  it("says the saved workflows couldn't load, then describes a new workflow", async () => {
    const error = vi.spyOn(toast, "error");
    try {
      const requests = listWorkflows([], "error");
      const { onStart, link } = renderMenu();
      await waitFor(() => expect(requests()).toBe(1));

      fireEvent.pointerDown(link, { button: 0 });
      await waitFor(() => expect(onStart).toHaveBeenCalledWith());
      expect(error).toHaveBeenCalledWith("Couldn't load your saved workflows.");
      expect(requests()).toBe(2);
    } finally {
      error.mockRestore();
    }
  });

  it("keeps the same focused button while the list loads", async () => {
    listWorkflows([triage]);
    const { link } = renderMenu();
    link.focus();

    await waitFor(() => expect(link).toHaveAttribute("aria-haspopup", "menu"));
    expect(screen.getByRole("button", { name: "run a workflow" })).toBe(link);
    expect(link).toHaveFocus();
  });
});
