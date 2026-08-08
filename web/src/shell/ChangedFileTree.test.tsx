import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RunnerOfflineError, type WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";
import { ChangedFileTree } from "./ChangedFileTree";

afterEach(cleanup);

function changedFile(
  path: string,
  status: WorkspaceChangedFile["status"] = "modified",
): WorkspaceChangedFile {
  return {
    bytes: 10,
    modified_at: null,
    name: path.split("/").at(-1) ?? path,
    path,
    status,
    // Required since main added per-file diff stats; the tree doesn't show them.
    lines_added: null,
    lines_removed: null,
  };
}

/** Render ChangedFileTree with defaults; the rows mount FileDownloadButton
 *  (imperative, no query on render) so a QueryClientProvider isn't required,
 *  but we wrap in one anyway to stay robust to future row dependencies. */
function renderTree(props: Partial<Parameters<typeof ChangedFileTree>[0]> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ChangedFileTree
        files={[]}
        isLoading={false}
        isError={false}
        error={null}
        onFileSelect={vi.fn()}
        showHidden={false}
        onShowHidden={vi.fn()}
        searchQuery=""
        sort="alpha"
        conversationId="conv_abc"
        {...props}
      />
    </QueryClientProvider>,
  );
}

describe("ChangedFileTree grouping", () => {
  it("groups changed files under their folders, fully expanded by default", () => {
    renderTree({
      files: [changedFile("src/a.ts"), changedFile("src/nested/b.ts"), changedFile("README.md")],
    });

    // Folder rows for each directory segment.
    expect(screen.getByRole("button", { name: "src/" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "nested/" })).toBeInTheDocument();
    // Leaf rows use the filename only (not the full path) and are visible
    // because folders start expanded.
    expect(screen.getByText("a.ts")).toBeInTheDocument();
    expect(screen.getByText("b.ts")).toBeInTheDocument();
    expect(screen.getByText("README.md")).toBeInTheDocument();
  });

  it("collapses and re-expands a folder on click", () => {
    renderTree({ files: [changedFile("src/a.ts"), changedFile("src/b.ts")] });

    const folder = screen.getByRole("button", { name: "src/" });
    expect(folder).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("a.ts")).toBeInTheDocument();

    fireEvent.click(folder);
    expect(folder).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("a.ts")).not.toBeInTheDocument();

    fireEvent.click(folder);
    expect(screen.getByText("a.ts")).toBeInTheDocument();
  });

  it("selects a file when its row is clicked", () => {
    const onFileSelect = vi.fn();
    renderTree({ files: [changedFile("src/a.ts")], onFileSelect });

    fireEvent.click(screen.getByText("a.ts"));
    expect(onFileSelect).toHaveBeenCalledWith("src/a.ts");
  });
});

describe("ChangedFileTree hidden files", () => {
  it("hides dot-directory files and offers to reveal them", () => {
    const onShowHidden = vi.fn();
    renderTree({
      files: [changedFile("visible.ts"), changedFile(".config/secret.ts")],
      onShowHidden,
    });

    expect(screen.getByText("visible.ts")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: ".config/" })).not.toBeInTheDocument();
    expect(screen.getByText(/1 file hidden/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /click to show/i }));
    expect(onShowHidden).toHaveBeenCalled();
  });

  it("shows hidden files when showHidden is set", () => {
    renderTree({
      files: [changedFile("visible.ts"), changedFile(".config/secret.ts")],
      showHidden: true,
    });

    expect(screen.getByRole("button", { name: ".config/" })).toBeInTheDocument();
    expect(screen.getByText("secret.ts")).toBeInTheDocument();
  });
});

describe("ChangedFileTree search", () => {
  it("filters to matching files and keeps their folders", () => {
    renderTree({
      files: [changedFile("src/alpha.ts"), changedFile("src/beta.ts")],
      searchQuery: "alpha",
    });

    expect(screen.getByText("alpha.ts")).toBeInTheDocument();
    expect(screen.queryByText("beta.ts")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "src/" })).toBeInTheDocument();
  });

  it("shows a no-match message when nothing matches", () => {
    renderTree({ files: [changedFile("src/alpha.ts")], searchQuery: "zzz" });
    expect(screen.getByText(/no changed files match/i)).toBeInTheDocument();
  });
});

describe("ChangedFileTree empty and error states", () => {
  it("shows the empty state with no changes", () => {
    renderTree({ files: [] });
    expect(screen.getByText(/no workspace changes yet/i)).toBeInTheDocument();
  });

  it("shows the reconnect hint when the runner went offline", () => {
    renderTree({ isError: true, error: new RunnerOfflineError(), runnerWentOffline: true });
    expect(screen.getByText(/agent is asleep/i)).toBeInTheDocument();
  });

  it("shows the empty state (not the asleep hint) for a session that hasn't started", () => {
    renderTree({ isError: true, error: new RunnerOfflineError(), runnerWentOffline: false });
    expect(screen.getByText(/no workspace changes yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/agent is asleep/i)).not.toBeInTheDocument();
  });
});
