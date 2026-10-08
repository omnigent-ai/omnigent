import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { copyTextMock } = vi.hoisted(() => ({ copyTextMock: vi.fn(() => Promise.resolve()) }));
vi.mock("@/lib/clipboard", () => ({ copyText: copyTextMock }));
import { TooltipProvider } from "@/components/ui/tooltip";
import { RunnerOfflineError, type WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";
import { ROW_STATUS_SLOT_CLASS } from "./fileStatusUtils";
import { FlatFileList } from "./FlatFileList";

afterEach(cleanup);
beforeEach(() => copyTextMock.mockClear());

/** Render FlatFileList with sensible defaults, overriding only what a test needs. */
function renderList(props: Partial<Parameters<typeof FlatFileList>[0]> = {}) {
  return render(
    <TooltipProvider>
      <FlatFileList
        files={undefined}
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
    </TooltipProvider>,
  );
}

describe("FlatFileList runner-offline state", () => {
  it("shows the reconnect hint when the runner went offline (session failed)", () => {
    // RunnerOfflineError = the changes fetch's 503. With runnerWentOffline
    // (session status "failed", e.g. host restarted) the panel shows the
    // reconnect hint, NOT the generic "Failed to load" branch.
    renderList({ isError: true, error: new RunnerOfflineError(), runnerWentOffline: true });

    expect(screen.getByText(/agent is asleep/i)).toBeInTheDocument();
    expect(screen.getByText(/send a message in the chat to reconnect/i)).toBeInTheDocument();
    // The raw error text must NOT appear for this recoverable state.
    expect(screen.queryByText(/failed to load/i)).not.toBeInTheDocument();
  });

  it("shows the empty state (not the asleep hint) for a new session that hasn't started", () => {
    // A brand-new session also 503s while its runner connects, but it never
    // went "failed" — runnerWentOffline is false, so it must read as the
    // normal empty state, not alarm the user that the agent is asleep.
    renderList({ isError: true, error: new RunnerOfflineError(), runnerWentOffline: false });

    expect(screen.getByText(/no workspace changes yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/agent is asleep/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/failed to load/i)).not.toBeInTheDocument();
  });

  it("still shows the raw error for a non-runner-offline failure", () => {
    // Generic errors keep the diagnostic "Failed to load: …" text so real
    // failures aren't masked by the reconnect hint.
    renderList({ isError: true, error: new Error("500 Internal Server Error") });

    expect(screen.getByText(/failed to load: 500 internal server error/i)).toBeInTheDocument();
    expect(screen.queryByText(/agent is asleep/i)).not.toBeInTheDocument();
  });
});

describe("FlatFileList status / download alignment", () => {
  it("shows file size using the same caption treatment as the Files tree", () => {
    renderList({
      files: [
        {
          path: "src/app.ts",
          name: "app.ts",
          status: "modified",
          bytes: 2048,
          modified_at: null,
          lines_added: null,
          lines_removed: null,
        },
      ],
    });

    expect(screen.getByText("2.0 KB")).toHaveClass("text-sm", "text-muted-foreground");
  });

  it("keeps status separate while actions replace the far-right file size", () => {
    renderList({
      files: [
        {
          path: "src/app.ts",
          name: "app.ts",
          status: "modified",
          bytes: 2048,
          modified_at: null,
          lines_added: null,
          lines_removed: null,
        },
      ],
    });

    const letter = screen.getByText("M");
    expect(letter.parentElement).toHaveClass(ROW_STATUS_SLOT_CLASS);

    const size = screen.getByText("2.0 KB");
    const slot = size.parentElement;
    expect(slot).toHaveClass("relative");
    expect(size).toHaveClass("group-hover:invisible");
    const download = screen.getByRole("button", { name: /download app\.ts/i });
    const overlay = download.closest("span.absolute") as HTMLElement | null;
    expect(overlay).not.toBeNull();
    expect(slot).toContainElement(overlay);
  });
});

describe("FlatFileList line-change counter", () => {
  it("renders +added and −removed when both counts are present", () => {
    renderList({
      files: [
        {
          path: "src/app.ts",
          name: "app.ts",
          status: "modified",
          bytes: 2048,
          modified_at: null,
          lines_added: 12,
          lines_removed: 3,
        },
      ],
    });

    expect(screen.getByText("+12")).toBeInTheDocument();
    expect(screen.getByText("−3")).toBeInTheDocument();
  });

  it("shows only −removed for a deleted file (added is 0)", () => {
    renderList({
      files: [
        {
          path: "gone.py",
          name: "gone.py",
          status: "deleted",
          bytes: null,
          modified_at: null,
          lines_added: 0,
          lines_removed: 7,
        },
      ],
    });

    expect(screen.getByText("−7")).toBeInTheDocument();
    // +0 still renders (0 is a real, non-null count) but the removed side is
    // the meaningful one for a deletion.
    expect(screen.getByText("+0")).toBeInTheDocument();
  });

  it("omits the counter entirely when both counts are null (binary/untracked/unavailable)", () => {
    renderList({
      files: [
        {
          path: "img.bin",
          name: "img.bin",
          status: "modified",
          bytes: 1024,
          modified_at: null,
          lines_added: null,
          lines_removed: null,
        },
      ],
    });

    expect(screen.queryByText(/^\+/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^−/)).not.toBeInTheDocument();
  });

  it("omits the counter for a mode-only change (both counts 0)", () => {
    // A chmod-only edit shows in numstat as 0/0; a "+0 −0" badge is noise, so
    // suppress it while still rendering a real deletion's −N.
    renderList({
      files: [
        {
          path: "script.sh",
          name: "script.sh",
          status: "modified",
          bytes: 512,
          modified_at: null,
          lines_added: 0,
          lines_removed: 0,
        },
      ],
    });

    expect(screen.queryByText(/^\+/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^−/)).not.toBeInTheDocument();
  });
});

describe("FlatFileList copy path", () => {
  it("copies the full path even though the button is named for the file", () => {
    // The accessible name carries only the basename (so a screen reader isn't
    // read a whole path per row, and the name can't collide with the folder
    // toggles that legitimately contain directory segments) -- but the
    // CLIPBOARD must get the complete path, which is the whole point.
    renderList({
      files: [
        {
          path: "src/deep/app.ts",
          name: "app.ts",
          status: "modified",
          bytes: 2048,
          modified_at: null,
          lines_added: null,
          lines_removed: null,
        },
      ],
    });

    const button = screen.getByRole("button", { name: "Copy path: app.ts" });
    fireEvent.click(button);

    expect(copyTextMock).toHaveBeenCalledWith("src/deep/app.ts");
  });
});

/** A single changed-file record with sensible defaults. */
function changedFile(path: string): WorkspaceChangedFile {
  return {
    path,
    name: path.split("/").at(-1) ?? path,
    status: "modified",
    bytes: 10,
    modified_at: null,
    lines_added: null,
    lines_removed: null,
  };
}

describe("FlatFileList limited-tracking notice", () => {
  it("replaces the empty state with the non-git notice when tracking is limited", () => {
    // A non-git workspace with no recorded edits: the empty list would read as
    // "no changes", so the notice must explain the limitation instead.
    renderList({ files: [], trackingComplete: false, trackingReason: "non_git_workspace" });

    expect(screen.getByText(/limited change tracking/i)).toBeInTheDocument();
    expect(screen.getByText(/isn't a git repository/i)).toBeInTheDocument();
    expect(screen.queryByText(/no workspace changes yet/i)).not.toBeInTheDocument();
  });

  it("shows the notice above the list when limited tracking still has some files", () => {
    // Non-git workspaces still surface the agent's own file-tool edits; the
    // notice warns those aren't the whole story (CLI/shell edits are missed).
    renderList({
      files: [changedFile("src/app.ts")],
      trackingComplete: false,
      trackingReason: "non_git_workspace",
    });

    expect(screen.getByText(/limited change tracking/i)).toBeInTheDocument();
    expect(screen.getByText("app.ts")).toBeInTheDocument();
  });

  it("keeps the notice when every change is in a hidden file", () => {
    renderList({
      files: [changedFile(".env")],
      trackingComplete: false,
      trackingReason: "non_git_workspace",
    });

    expect(screen.getByText(/limited change tracking/i)).toBeInTheDocument();
    expect(screen.getByText(/all changes are in hidden files/i)).toBeInTheDocument();
  });

  it("keeps the notice when the search matches no changed file", () => {
    renderList({
      files: [changedFile("src/app.ts")],
      searchQuery: "zzz",
      trackingComplete: false,
      trackingReason: "non_git_workspace",
    });

    expect(screen.getByText(/limited change tracking/i)).toBeInTheDocument();
    expect(screen.getByText(/no changed files match "zzz"/i)).toBeInTheDocument();
  });

  it("uses the no-workspace copy for the no_workspace reason", () => {
    renderList({ files: [], trackingComplete: false, trackingReason: "no_workspace" });

    expect(screen.getByText(/limited change tracking/i)).toBeInTheDocument();
    expect(screen.getByText(/no tracked workspace/i)).toBeInTheDocument();
  });

  it("uses a neutral fallback when the runner gives no recognized reason", () => {
    // An unrecognized reason still means tracking is incomplete, but the UI
    // must not guess at a cause such as "isn't a Git repository".
    renderList({ files: [], trackingComplete: false, trackingReason: null });

    expect(screen.getByText(/limited change tracking/i)).toBeInTheDocument();
    expect(screen.getByText(/some edits may not be listed here/i)).toBeInTheDocument();
    expect(screen.queryByText(/isn't a git repository/i)).not.toBeInTheDocument();
  });

  it("shows the normal empty state (no notice) when tracking is complete", () => {
    // Git workspaces report complete tracking, so an empty list genuinely
    // means nothing changed — keep the plain empty state.
    renderList({ files: [], trackingComplete: true });

    expect(screen.getByText(/no workspace changes yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/limited change tracking/i)).not.toBeInTheDocument();
  });
});
