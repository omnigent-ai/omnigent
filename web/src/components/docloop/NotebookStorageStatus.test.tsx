import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NotebookStorageStatus } from "./NotebookStorageStatus";
import { mountNotebookPane } from "./notebook-pane.mjs";

const fetcher = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch: fetcher }));
afterEach(cleanup);
const binding = "b".repeat(64);
function snapshot(revision = "a", status = "recorded", source = "original") {
  return {
    schema_version: 1,
    session_id: "storage",
    binding_id: binding,
    revision: revision.repeat(64),
    format: "org",
    document_name: "agent.org",
    history_status: { status },
    nodes: [{ id: "note", kind: "markdown", source, editable: true }],
  };
}
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });

describe("explicit history-only recovery", () => {
  it.each(["pending", "recorded"])(
    "retains a partial-write draft and allows explicit recovery with history %s",
    async (historyStatus) => {
      let current = snapshot();
      const calls: { url: string; method: string; body?: string }[] = [];
      const fetch = async (url: string, init?: RequestInit) => {
        calls.push({ url, method: init?.method || "GET", body: init?.body as string });
        if (init?.method === "PATCH") {
          current = snapshot("c", historyStatus, "my retained draft");
          return response(
            {
              code: "save_failed",
              error: "Document updated; history pending.",
              installed: true,
              retry_action: false,
              current_revision: current.revision,
            },
            503,
          );
        }
        if (init?.method === "POST") {
          expect(JSON.parse(init.body as string)).toEqual({
            binding_id: binding,
            revision: "c".repeat(64),
          });
          current = snapshot("c", "recorded", "my retained draft");
        }
        return response(current);
      };
      const host = document.createElement("div");
      document.body.append(host);
      const pane = mountNotebookPane(host, {
        sessionId: "storage",
        fetcher: fetch,
        pollMs: 100000,
      });
      try {
        const ui = within(host.shadowRoot as unknown as HTMLElement);
        const input = await ui.findByRole("textbox", { name: "Source note" });
        fireEvent.input(input, { target: { value: "my retained draft" } });
        fireEvent.click(ui.getByRole("button", { name: "Save" }));
        await waitFor(() =>
          expect(ui.getByText(/Current state is loaded for review/)).toBeVisible(),
        );
        expect(input).toHaveValue("my retained draft");
        expect(ui.getByText(/revision cccccccccccc/)).toBeVisible();
        expect(ui.getByRole("button", { name: "Save" })).toBeDisabled();
        expect(calls.filter((c) => c.method === "PATCH")).toHaveLength(1);
        fireEvent.click(ui.getByRole("button", { name: "Repair version history" }));
        await ui.findByText(/Version history verified. No edit or execution was repeated/);
        expect(input).toHaveValue("my retained draft");
        expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
        expect(calls.filter((c) => c.method === "PATCH")).toHaveLength(1);
      } finally {
        pane.dispose();
        host.remove();
      }
    },
  );

  it("uses current binding and revision for native-view recovery without an execution call", async () => {
    fetcher.mockImplementation(async (_url: string, init?: RequestInit) =>
      response(snapshot("a", init?.method === "POST" ? "recorded" : "pending")),
    );
    render(<NotebookStorageStatus sessionId="storage" active />);
    fireEvent.click(await screen.findByRole("button", { name: "Repair version history" }));
    await screen.findByText(/Version history verified. No edit, cell execution/);
    const writes = fetcher.mock.calls.filter(([, init]) => init?.method === "POST");
    expect(writes).toHaveLength(1);
    expect(writes[0][0]).toBe("/v1/sessions/storage/docloop/recover-history");
    expect(JSON.parse(writes[0][1].body)).toEqual({
      revision: "a".repeat(64),
      binding_id: binding,
    });
  });
});
