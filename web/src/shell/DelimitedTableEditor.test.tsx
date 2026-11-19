import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";
import { fetchFileContent } from "@/hooks/useFileContent";
import { useWorkspaceServeable } from "@/hooks/useWorkspaceChangedFiles";
import { DelimitedTableEditor } from "./DelimitedTableEditor";

const write = vi.hoisted(() =>
  vi.fn<(input: { path: string; content: string }) => Promise<void>>(),
);
vi.mock("@/hooks/useWriteFileContent", async () => {
  const { useMutation } = await import("@tanstack/react-query");
  return {
    useWriteFileContent: () =>
      useMutation({ mutationFn: (input: { path: string; content: string }) => write(input) }),
  };
});
vi.mock("@/hooks/useFileContent", () => ({ fetchFileContent: vi.fn() }));
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({ useWorkspaceServeable: vi.fn(() => true) }));

const SOURCE = "name,units\r\nWest,10\r\nEast,2\r\nNorth,3\r\n";
let serverContent = SOURCE;
function editor(props: Partial<ComponentProps<typeof DelimitedTableEditor>> = {}) {
  return (
    <DelimitedTableEditor
      conversationId="conv_table_edit"
      path="sales.csv"
      content={SOURCE}
      encoding="utf-8"
      truncated={false}
      canEdit
      {...props}
    />
  );
}
function renderEditor(props: Partial<ComponentProps<typeof DelimitedTableEditor>> = {}) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return render(editor(props), {
    wrapper: ({ children }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
}
function applyCell(before: string, after: string) {
  fireEvent.doubleClick(screen.getByRole("cell", { name: before }));
  fireEvent.change(screen.getByRole("textbox", { name: "Cell value" }), {
    target: { value: after },
  });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
}
async function saveNow() {
  fireEvent.click(screen.getByRole("button", { name: "Save now" }));
  await waitFor(() => expect(write).toHaveBeenCalled());
}

beforeEach(() => {
  serverContent = SOURCE;
  write.mockReset();
  write.mockImplementation(async ({ content }) => {
    serverContent = content;
  });
  vi.mocked(useWorkspaceServeable).mockReturnValue(true);
  vi.mocked(fetchFileContent).mockImplementation(async (_session, path) => ({
    object: "session.environment.filesystem.file_content",
    path,
    content_type: "text/csv",
    encoding: "utf-8",
    content: serverContent,
    bytes: serverContent.length,
  }));
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe("DelimitedTableEditor", () => {
  it.each(["sales.csv", "sales.tsv"])(
    "saves the file-order cell after sorting and filtering %s",
    async (path) => {
      const source = path.endsWith(".tsv") ? SOURCE.replaceAll(",", "\t") : SOURCE;
      serverContent = source;
      const onDirtyChange = vi.fn();
      renderEditor({ path, content: source, onDirtyChange });
      fireEvent.click(screen.getByRole("button", { name: "Sort by units" }));
      fireEvent.change(screen.getByRole("textbox", { name: "Filter name" }), {
        target: { value: "st" },
      });
      fireEvent.click(screen.getByRole("cell", { name: "2" }));
      fireEvent.click(screen.getByRole("button", { name: "Edit cell" }));
      fireEvent.change(screen.getByRole("textbox", { name: "Cell value" }), {
        target: { value: "20" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Apply" }));
      expect(onDirtyChange).toHaveBeenLastCalledWith(true);
      expect(screen.getByRole("cell", { name: "20" })).toBeInTheDocument();
      await saveNow();
      expect(write).toHaveBeenCalledExactlyOnceWith({
        path,
        content: source.replace(
          "East" + (path.endsWith(".tsv") ? "\t" : ",") + "2",
          "East" + (path.endsWith(".tsv") ? "\t" : ",") + "20",
        ),
      });
      await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(false));
    },
  );

  it("autosaves an applied edit and lets the last edit be undone", async () => {
    vi.useFakeTimers();
    renderEditor();
    applyCell("West", "West, coast");
    expect(write).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1001);
    });
    expect(write).toHaveBeenCalledExactlyOnceWith({
      path: "sales.csv",
      content: SOURCE.replace("West", '"West, coast"'),
    });
    fireEvent.click(screen.getByRole("button", { name: "Undo last edit" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1001);
    });
    expect(write).toHaveBeenLastCalledWith({ path: "sales.csv", content: SOURCE });
  });

  it("cancels a cell edit without changing the file", () => {
    renderEditor();
    fireEvent.focus(screen.getByRole("cell", { name: "West" }));
    fireEvent.keyDown(screen.getByRole("cell", { name: "West" }), { key: "Enter" });
    fireEvent.change(screen.getByRole("textbox", { name: "Cell value" }), {
      target: { value: "cancelled" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("cell", { name: "West" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save now" })).toBeNull();
    expect(write).not.toHaveBeenCalled();
  });

  it.each(["keep", "load"])(
    "detects an external edit before saving and lets the user %s",
    async (choice) => {
      renderEditor();
      applyCell("West", "Local");
      serverContent = SOURCE.replace("West", "External");
      fireEvent.click(screen.getByRole("button", { name: "Save now" }));
      expect(await screen.findByRole("alert")).toHaveTextContent(/modified externally/);
      expect(write).not.toHaveBeenCalled();
      if (choice === "keep") {
        fireEvent.click(screen.getByRole("button", { name: "Keep mine" }));
        await waitFor(() =>
          expect(write).toHaveBeenCalledWith({
            path: "sales.csv",
            content: SOURCE.replace("West", "Local"),
          }),
        );
      } else {
        fireEvent.click(screen.getByRole("button", { name: "Load latest" }));
        expect(screen.getByRole("cell", { name: "External" })).toBeInTheDocument();
        expect(write).not.toHaveBeenCalled();
      }
    },
  );

  it("keeps edits visible after a failed save and retries", async () => {
    write.mockRejectedValueOnce(new Error("unavailable"));
    renderEditor();
    applyCell("West", "Local");
    fireEvent.click(screen.getByRole("button", { name: "Save now" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not be saved/);
    expect(screen.getByRole("cell", { name: "Local" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry save" }));
    await waitFor(() => expect(write).toHaveBeenCalledTimes(2));
    expect(write).toHaveBeenLastCalledWith({
      path: "sales.csv",
      content: SOURCE.replace("West", "Local"),
    });
  });

  it("buffers applied edits offline and saves when the workspace reconnects", async () => {
    vi.mocked(useWorkspaceServeable).mockReturnValue(false);
    const view = renderEditor();
    applyCell("West", "Local");
    expect(screen.getByRole("button", { name: "Save now" })).toBeDisabled();
    expect(write).not.toHaveBeenCalled();
    vi.mocked(useWorkspaceServeable).mockReturnValue(true);
    view.rerender(editor());
    await waitFor(() =>
      expect(write).toHaveBeenCalledWith({
        path: "sales.csv",
        content: SOURCE.replace("West", "Local"),
      }),
    );
  });

  it.each([
    { canEdit: false },
    { truncated: true },
    { encoding: "base64" as const, content: btoa(SOURCE) },
    { content: "a,b\n1\n" },
    { path: "sales.xlsx" },
    { path: "sales.parquet" },
  ])("keeps unsupported previews read-only (%j)", (props) => {
    renderEditor(props);
    fireEvent.click(screen.getAllByRole("cell")[0]);
    expect(screen.queryByRole("button", { name: "Edit cell" })).toBeNull();
    fireEvent.doubleClick(screen.getAllByRole("cell")[0]);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(write).not.toHaveBeenCalled();
  });

  it("does not save a pending edit after edit permission is revoked", async () => {
    vi.useFakeTimers();
    const view = renderEditor();
    applyCell("West", "Local");
    view.rerender(editor({ canEdit: false }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1001);
    });
    expect(write).not.toHaveBeenCalled();
  });

  it("updates a clean table from disk without applying an open dialog to different rows", () => {
    const view = renderEditor();
    fireEvent.doubleClick(screen.getByRole("cell", { name: "West" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Cell value" }), {
      target: { value: "Local" },
    });
    view.rerender(editor({ content: SOURCE.replace("West", "External") }));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(screen.getByRole("alert")).toHaveTextContent(/file changed while this cell was open/);
    expect(write).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("cell", { name: "External" })).toBeInTheDocument();
  });
});
