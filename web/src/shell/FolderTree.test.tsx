import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { copyTextMock } = vi.hoisted(() => ({ copyTextMock: vi.fn(() => Promise.resolve()) }));
vi.mock("@/lib/clipboard", () => ({ copyText: copyTextMock }));

// Drive lazy-directory listings from a fixture so the tree's central
// `useWorkspaceDirectories` controller resolves nested lazy dirs without a
// network. `lazyChildren` maps a dir path to the entries the "runner" would
// return; the hook returns a Map only for the paths the tree currently asks
// for, so descending into a deeper level requires the shallower level's data
// to already be present — exactly the incremental-widening path.
const { lazyChildren, lazyErrors } = vi.hoisted(() => ({
  lazyChildren: new Map<string, unknown[]>(),
  lazyErrors: new Set<string>(),
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkspaceChangedFilesModule>()),
  useWorkspaceDirectories: (_c: string | undefined, dirPaths: string[]) => {
    const map = new Map();
    for (const p of dirPaths) {
      const errored = lazyErrors.has(p);
      map.set(p, {
        data: lazyChildren.get(p),
        isLoading: !errored && !lazyChildren.has(p),
        isError: errored,
      });
    }
    return map;
  },
}));
import { RunnerOfflineError, type WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import type * as WorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import {
  ROW_ACTION_SIZE_CLASS,
  ROW_META_SLOT_CLASS,
  ROW_STATUS_SLOT_CLASS,
} from "./fileStatusUtils";
import { FolderTree } from "./FolderTree";

afterEach(cleanup);
beforeEach(() => copyTextMock.mockClear());

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

  it("puts a folder's dirty dot and a file's status letter in one shared column", () => {
    // The two git-status markers must land in the same x down the tree. Each
    // sized to its own content instead put them ~4px apart: the dot had a
    // fixed 22px box while the letter was a variable-width badge centred on
    // itself. Both now centre in ROW_STATUS_SLOT_CLASS.
    renderTree({
      files: [dir("src"), file("app.ts")],
      changedFiles: [
        {
          path: "src/app.ts",
          name: "app.ts",
          status: "modified",
          bytes: 1,
          modified_at: null,
          lines_added: null,
          lines_removed: null,
        },
        {
          path: "app.ts",
          name: "app.ts",
          status: "created",
          bytes: 1,
          modified_at: null,
          lines_added: null,
          lines_removed: null,
        },
      ],
    });

    const dotSlot = screen.getByText("●").parentElement;
    const letterSlot = screen.getByTitle("Added").parentElement;
    for (const slot of [dotSlot, letterSlot]) {
      expect(slot).toHaveClass(ROW_STATUS_SLOT_CLASS);
      expect(slot).toHaveClass("justify-center");
    }
  });
});

describe("FolderTree trailing column", () => {
  it("gives folders and files alike the same fixed-width trailing slot", () => {
    // The size label is variable width ("985 B" vs "463 KB"). Letting it size
    // the column dragged the copy button, the download button and the status
    // marker to a different x on every row (~16px of measured drift). Every
    // row -- including folders, which show no size at all -- now renders the
    // same fixed slot, so those controls line up in one column.
    renderTree({ files: [file("tiny.txt", 985), file("huge.json", 474_000), dir("src")] });

    const copyButtons = screen.getAllByRole("button", { name: /^Copy (path|folder path):/ });
    expect(copyButtons).toHaveLength(3);
    for (const button of copyButtons) {
      // The copy button lives inside the fixed-width trailing column...
      const slot = button.closest(`.${ROW_META_SLOT_CLASS}`);
      expect(slot, "every row's copy button must sit in the trailing column").not.toBeNull();
      // ...paired with the download button on its LEFT (copy is the rightmost
      // control), or with a spacer standing in for the download where there is
      // none (folders, deleted files) so the pair keeps one x on every row.
      const beside = button.previousElementSibling;
      expect(beside, "the copy button must be preceded by its pair").not.toBeNull();
      const isDownload = beside?.getAttribute("aria-label")?.startsWith("Download");
      const isReservedSpacer = beside?.classList.contains(ROW_ACTION_SIZE_CLASS);
      expect(
        isDownload || isReservedSpacer,
        "copy must sit immediately right of the download button (or its reserved footprint)",
      ).toBe(true);
    }
  });

  it("offers a copy button on directory rows, not just files", () => {
    // Directories are paths worth copying too. The row used to be a single
    // <button>, so a copy control could not be nested inside it at all --
    // hence the wrapper-div restructure.
    renderTree({ files: [dir("src")] });

    fireEvent.click(screen.getByRole("button", { name: "Copy folder path: src" }));

    expect(copyTextMock).toHaveBeenCalledWith("src");
  });
});

describe("FolderTree double-click to open a folder", () => {
  it("re-roots onto the folder on double click, but only expands on a single click", () => {
    // Finder's contract. A single click must keep working as the expand
    // toggle -- if double click stole it, the tree would be unusable.
    const onNavigateDir = vi.fn();
    renderTree({ files: [dir("src"), file("README.md")], onNavigateDir });

    const folder = screen.getByRole("button", { name: "src/" });

    fireEvent.click(folder);
    expect(onNavigateDir, "a single click must not re-root").not.toHaveBeenCalled();

    fireEvent.doubleClick(folder);
    expect(onNavigateDir).toHaveBeenCalledWith("src");
  });

  it("passes the path relative to the browsed root, not the bare folder name", () => {
    // Nested rows must report their full path from the current root, or
    // re-rooting from a deep row would land in the wrong directory.
    const onNavigateDir = vi.fn();
    renderTree({ files: [file("src/components/Button.tsx")], onNavigateDir });

    fireEvent.doubleClick(screen.getByRole("button", { name: "components/" }));

    expect(onNavigateDir).toHaveBeenCalledWith("src/components");
  });

  it("leaves folders inert when no navigate handler is supplied", () => {
    // The prop is optional; without it the row must still expand normally.
    renderTree({ files: [dir("src")] });

    const folder = screen.getByRole("button", { name: "src/" });
    fireEvent.doubleClick(folder);

    expect(folder).toBeInTheDocument();
  });
});

describe("FolderTree nested lazy loading", () => {
  beforeEach(() => {
    lazyChildren.clear();
    lazyErrors.clear();
  });

  it("shows an error row when a lazy directory's listing fails", async () => {
    lazyErrors.add("src");
    renderTree({ files: [dir("src")], conversationId: "conv_lazy_error" });

    fireEvent.click(screen.getByRole("button", { name: "src/" }));

    expect(await screen.findByText("Failed to load this folder.")).toBeInTheDocument();
  });

  it("loads a deep lazy level on a restored multi-level expansion (no per-level clicks)", async () => {
    // Regression guard for B1. The bug only shows on RESTORE/RE-ROOT, where a
    // multi-level expansion is seeded at once rather than clicked open level by
    // level — interactive expansion masks it, because each click mutates
    // expandedPaths and re-runs the fetch-set computation anyway. Here we build
    // the expansion with clicks (which caches src + src/deep as expanded), then
    // REMOUNT: the fresh tree seeds both expanded paths from the cache in one
    // shot, and its fetch set starts from nothing. The controller must widen
    // past the first level on its own as src's listing lands, or src/deep never
    // fetches and renders as an empty folder. A fetch-set computation that
    // ignores freshly-arrived data resolves only src and leaves leaf.ts missing.
    const conversationId = "conv_restore_deep";
    lazyChildren.set("src", [dir("src/deep")]);
    lazyChildren.set("src/deep", [file("src/deep/leaf.ts", 42)]);

    const { unmount } = renderTree({ files: [dir("src")], conversationId });
    fireEvent.click(screen.getByRole("button", { name: "src/" }));
    fireEvent.click(await screen.findByRole("button", { name: "deep/" }));
    expect(await screen.findByText("leaf.ts")).toBeInTheDocument();

    // Restore: remount the same conversation. Expansion comes back from the
    // cache with no clicks; the deep level must fetch and render on its own.
    unmount();
    renderTree({ files: [dir("src")], conversationId });
    expect(await screen.findByText("leaf.ts")).toBeInTheDocument();
  });
});

describe("FolderTree default expansion on conversation switch", () => {
  const baseProps = {
    isLoading: false,
    isError: false,
    error: null,
    onFileSelect: vi.fn(),
    showHidden: false,
    changedFiles: undefined,
    sort: "alpha" as const,
  };

  it("seeds a switched-to conversation's expansion from its own files, not the previous one's", () => {
    // Regression guard: FolderTree isn't keyed by conversation, so a switch
    // re-renders the same instance and a layout effect seeds the new
    // conversation's default-expansion cache. The All-files query intentionally
    // drops cross-key placeholderData, so on a real switch `files` goes
    // undefined before the new conversation's list arrives — the effect must
    // early-return on that undefined frame and then seed defaults from the NEW
    // conversation's files. (A cross-key placeholder would instead leave the
    // previous conversation's files on screen under the new key, seeding it
    // with the wrong auto-expanded folders for the rest of the session.)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = (conversationId: string, files: WorkspaceFile[] | undefined) => (
      <QueryClientProvider client={client}>
        <FolderTree {...baseProps} conversationId={conversationId} files={files} />
      </QueryClientProvider>
    );

    // Conversation A: a nested file makes its intermediate dir auto-expand.
    const { rerender } = render(view("conv_A", [file("alpha/a.ts")]));
    expect(screen.getByText("a.ts")).toBeInTheDocument();

    // Switch to B: with no cross-key placeholder the files blank to undefined
    // first, then B's real files arrive.
    rerender(view("conv_B", undefined));
    rerender(view("conv_B", [file("beta/b.ts")]));

    // B auto-expands its OWN nested dir; A's folder is gone entirely.
    expect(screen.getByText("b.ts")).toBeInTheDocument();
    expect(screen.queryByText("a.ts")).toBeNull();
    expect(screen.getByRole("button", { name: "beta/" })).toHaveAttribute("aria-expanded", "true");
  });
});
