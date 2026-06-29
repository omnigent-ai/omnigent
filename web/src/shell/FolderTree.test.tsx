import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RunnerOfflineError, type WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import { FolderTree } from "./FolderTree";

afterEach(cleanup);

/** Render FolderTree (the "All" files tab) with defaults, overriding per test.
 *  Wrapped in a QueryClientProvider because rendered rows call
 *  `useWorkspaceDirectory` (a TanStack query) for lazy subdirectory loading. */
function renderTree(props: Partial<Parameters<typeof FolderTree>[0]> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <FolderTree
        files={undefined}
        isLoading={false}
        isError={false}
        error={null}
        onFileSelect={vi.fn()}
        conversationId="conv_abc"
        showHidden={false}
        changedFiles={undefined}
        sort="alpha"
        {...props}
      />
    </QueryClientProvider>,
  );
}

function file(path: string, bytes = 10, modifiedAt: number | null = null): WorkspaceFile {
  return {
    bytes,
    modified_at: modifiedAt,
    name: path.split("/").at(-1) ?? path,
    path,
    type: "file",
  };
}

function dir(path: string, modifiedAt: number | null = null): WorkspaceFile {
  return {
    bytes: null,
    modified_at: modifiedAt,
    name: path.split("/").at(-1) ?? path,
    path,
    type: "directory",
  };
}

describe("FolderTree runner-offline state", () => {
  it("shows the reconnect hint when the runner went offline (session failed)", () => {
    // With runnerWentOffline the "All" tab shows the same reconnect hint as
    // the Changed tab, not the generic "Failed to load".
    renderTree({ isError: true, error: new RunnerOfflineError(), runnerWentOffline: true });

    expect(screen.getByText(/agent is asleep/i)).toBeInTheDocument();
    expect(screen.getByText(/send a message in the chat to reconnect/i)).toBeInTheDocument();
    expect(screen.queryByText(/failed to load/i)).not.toBeInTheDocument();
  });

  it("shows the empty state (not the asleep hint) for a new session that hasn't started", () => {
    // A new session 503s while connecting but never went "failed" — show
    // the normal empty state, not the asleep alarm.
    renderTree({ isError: true, error: new RunnerOfflineError(), runnerWentOffline: false });

    expect(screen.getByText(/no files in workspace/i)).toBeInTheDocument();
    expect(screen.queryByText(/agent is asleep/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/failed to load/i)).not.toBeInTheDocument();
  });

  it("still shows the raw error for a non-runner-offline failure", () => {
    renderTree({ isError: true, error: new Error("500 Internal Server Error") });

    expect(screen.getByText(/failed to load: 500 internal server error/i)).toBeInTheDocument();
    expect(screen.queryByText(/agent is asleep/i)).not.toBeInTheDocument();
  });
});

describe("FolderTree sorting", () => {
  it("groups directories ahead of files, then sorts files by name", () => {
    renderTree({
      files: [file("zzz.txt"), dir("mmm"), file("aaa.txt")],
      sort: "alpha",
    });
    const order = screen.getAllByText(/^(aaa\.txt|mmm\/|zzz\.txt)$/).map((el) => el.textContent);
    // Folder first (even though "mmm" sorts after "aaa"), then files by name.
    expect(order).toEqual(["mmm/", "aaa.txt", "zzz.txt"]);
  });

  it("sorts directories among themselves by last edited, still ahead of files", () => {
    renderTree({
      files: [dir("olddir", 100), file("recent.txt", 10, 200), dir("newdir", 300)],
      sort: "recent",
    });
    const order = screen
      .getAllByText(/^(olddir\/|newdir\/|recent\.txt)$/)
      .map((el) => el.textContent);
    // Directories first (newest among themselves), so the file trails even
    // though its mtime is newer than olddir's.
    expect(order).toEqual(["newdir/", "olddir/", "recent.txt"]);
  });

  it("sorts files by size (largest first), with directories grouped first", () => {
    renderTree({
      files: [file("small.txt", 5), file("big.txt", 500), dir("zdir"), file("mid.txt", 50)],
      sort: "size",
    });
    const order = screen
      .getAllByText(/^(zdir\/|big\.txt|mid\.txt|small\.txt)$/)
      .map((el) => el.textContent);
    expect(order).toEqual(["zdir/", "big.txt", "mid.txt", "small.txt"]);
  });

  it("sorts files by extension when sort is 'type'", () => {
    renderTree({
      files: [file("b.txt"), file("a.md"), file("c.js")],
      sort: "type",
    });
    const order = screen.getAllByText(/^(a\.md|b\.txt|c\.js)$/).map((el) => el.textContent);
    // Extensions sort ascending: js < md < txt.
    expect(order).toEqual(["c.js", "a.md", "b.txt"]);
  });
});

describe("FolderTree file size / download alignment", () => {
  it("overlays the download button on the file size so both share one slot", () => {
    // The size label and the hover download button must occupy the same
    // relative container: the size reserves the width and the button overlays
    // it (absolute inset-0), so the button appears exactly where the size was.
    renderTree({ files: [file("readme.md", 2048)] });

    const size = screen.getByText("2.0 KB");
    const slot = size.parentElement;
    expect(slot).toHaveClass("relative");
    // Size hides on hover but keeps its width to avoid a layout shift.
    expect(size).toHaveClass("group-hover:invisible");

    const download = screen.getByRole("button", { name: /download readme\.md/i });
    // Button sits in an absolutely-positioned overlay inside the same slot.
    const overlay = download.closest("span.absolute") as HTMLElement | null;
    expect(overlay).not.toBeNull();
    expect(slot).toContainElement(overlay);
  });

  it("renders the dirty-directory dot in a fixed-width slot matching the download column", () => {
    // The directory status dot must align with the file rows' download button
    // column, so it lives in a fixed-width (w-[22px]) centered container.
    renderTree({
      files: [dir("src")],
      changedFiles: [
        { path: "src/app.ts", name: "app.ts", status: "modified", bytes: 1, modified_at: null },
      ],
    });

    const dot = screen.getByText("●");
    const slot = dot.parentElement;
    expect(slot).toHaveClass("w-[22px]");
  });
});
