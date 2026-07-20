import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { RunnerOfflineError } from "@/hooks/useWorkspaceChangedFiles";
import { FlatFileList } from "./FlatFileList";

afterEach(cleanup);

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
  it("does not show a file-size label", () => {
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

    expect(screen.queryByText(/\bKB\b/)).not.toBeInTheDocument();
  });

  it("overlays the download button on the status letter so both share one slot", () => {
    // The status letter and the hover download button occupy the same relative
    // container: the letter reserves the width and the button overlays it
    // (absolute inset-0), so the button appears exactly where the letter was.
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
    const slot = letter.parentElement;
    expect(slot).toHaveClass("relative");
    // The letter hides on hover but keeps its width to avoid a layout shift.
    expect(letter).toHaveClass("group-hover:invisible");

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
