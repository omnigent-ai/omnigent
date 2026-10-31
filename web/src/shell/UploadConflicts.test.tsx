import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { cancelUpload, retryUpload } from "@/lib/workspaceUploadController";
import { useUploadStore, type UploadRow } from "@/store/uploadStore";
import { UploadConflicts } from "./UploadConflicts";

vi.mock("@/lib/workspaceUploadController", () => ({ cancelUpload: vi.fn(), retryUpload: vi.fn() }));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  useUploadStore.setState({ rows: {} });
});
it.each(["Replace", "Keep both", "Skip"])(
  "aggregates a batch into one conflict dialog and applies %s",
  (choice) => {
    const rows = ["a.txt", "b.txt"].map(
      (name) => ({ key: name, name, batch: "one", status: "conflict" }) as UploadRow,
    );
    useUploadStore.getState().add(rows);
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <UploadConflicts />
      </QueryClientProvider>,
    );
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(screen.getByText("a.txt")).toBeTruthy();
    expect(screen.getByText("b.txt")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: choice }));
    for (const row of rows) {
      if (choice === "Skip") expect(cancelUpload).toHaveBeenCalledWith(row.key);
      else
        expect(retryUpload).toHaveBeenCalledWith(
          row.key,
          client,
          choice === "Replace" ? "replace" : "rename",
        );
    }
  },
);
it("waits for batch creates before opening the dialog", () => {
  useUploadStore.getState().add([
    { key: "a", name: "a", batch: "one", status: "conflict" },
    { key: "b", name: "b", batch: "one", status: "connecting" },
  ] as UploadRow[]);
  const client = new QueryClient();
  render(
    <QueryClientProvider client={client}>
      <UploadConflicts />
    </QueryClientProvider>,
  );
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("applies a choice only to the first conflict when Apply to all is unchecked", () => {
  useUploadStore.getState().add([
    { key: "a", name: "a", batch: "one", status: "conflict" },
    { key: "b", name: "b", batch: "one", status: "conflict" },
  ] as UploadRow[]);
  const client = new QueryClient();
  render(
    <QueryClientProvider client={client}>
      <UploadConflicts />
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole("checkbox", { name: "Apply to all" }));
  fireEvent.click(screen.getByRole("button", { name: "Replace" }));
  expect(retryUpload).toHaveBeenCalledExactlyOnceWith("a", client, "replace");
});

it.each([1, 2])("uses grammatical conflict copy for %s files", (count) => {
  useUploadStore.getState().add(
    Array.from(
      { length: count },
      (_, index) =>
        ({
          key: String(index),
          name: `file${index}`,
          batch: "one",
          status: "conflict",
        }) as UploadRow,
    ),
  );
  render(
    <QueryClientProvider client={new QueryClient()}>
      <UploadConflicts />
    </QueryClientProvider>,
  );
  expect(screen.getByRole("dialog")).toHaveAccessibleDescription(
    `${count} ${count === 1 ? "file has" : "files have"} the same name in the destination. Choose how to upload ${count === 1 ? "it" : "them"}.`,
  );
});
