import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { NotebookHistory } from "./NotebookHistory";
import { prettyJSON } from "./display-json.mjs";

const fetcher = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch: fetcher }));

describe("saved notebook views", () => {
  it("opens an escaped read-only copy using only GET requests", async () => {
    const commit = "a".repeat(40),
      binding = "b".repeat(64);
    fetcher.mockImplementation(
      async (url: string) =>
        new Response(
          JSON.stringify(
            url.endsWith(commit)
              ? {
                  schema_version: 1,
                  session_id: "test",
                  binding_id: binding,
                  commit,
                  read_only: true,
                  document: {
                    document_name: "notes.org",
                    nodes: [
                      {
                        id: "old",
                        kind: "raw",
                        language: "json",
                        source: '{"id":9223372036854775807,"text":"<b>café</b>"}',
                        source_truncated: false,
                        output_text: "saved output",
                        output_truncated: false,
                      },
                    ],
                  },
                }
              : {
                  schema_version: 1,
                  session_id: "test",
                  binding_id: binding,
                  versions: [{ commit, message: "Initial note", date: "2026-09-10T00:00:00Z" }],
                  next_before: null,
                },
          ),
          { status: 200 },
        ),
    );
    const view = render(<NotebookHistory sessionId="test" />);
    fireEvent.click(await screen.findByRole("button", { name: /Initial note/ }));
    expect(await screen.findByText(/Saved copy · read only/)).toBeVisible();
    expect(screen.getByText(/9223372036854775807/).textContent).toContain(
      '\n  "id": 9223372036854775807',
    );
    expect(view.container.querySelector("b")).toBeNull();
    expect(screen.getByLabelText("Saved output old")).toHaveTextContent("saved output");
    expect(view.container.querySelector("textarea,input")).toBeNull();
    for (const [, options] of fetcher.mock.calls) expect(options.method).toBeUndefined();
    view.unmount();
  });

  it("rejects a saved copy from a changed notebook binding", async () => {
    const commit = "a".repeat(40);
    fetcher.mockImplementation(
      async (url: string) =>
        new Response(
          JSON.stringify(
            url.endsWith(commit)
              ? {
                  session_id: "test",
                  commit,
                  binding_id: "c".repeat(64),
                  read_only: true,
                }
              : {
                  schema_version: 1,
                  session_id: "test",
                  binding_id: "b".repeat(64),
                  versions: [{ commit, message: "Saved", date: "2026-09-10T00:00:00Z" }],
                  next_before: null,
                },
          ),
        ),
    );
    const view = render(<NotebookHistory sessionId="test" />);
    fireEvent.click(await screen.findByRole("button", { name: /Saved/ }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("Notebook binding changed"),
    );
    view.unmount();
  });

  it("formats JSON without rounding numbers or removing duplicate keys", () => {
    const text = '{"id":9223372036854775807,"id":1e999,"nested":[{},"a,\\\"b"]}';
    const formatted = prettyJSON(text);
    expect(formatted).toContain('"id": 9223372036854775807');
    expect(formatted).toContain('"id": 1e999');
    expect(formatted).toContain('"a,\\\"b"');
    expect(prettyJSON("unstructured text")).toBe("unstructured text");
  });
});
