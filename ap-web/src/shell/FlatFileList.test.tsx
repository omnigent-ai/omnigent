import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { RunnerOfflineError, type WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";
import { FlatFileList } from "./FlatFileList";

afterEach(cleanup);

/** A single changed-file record with sensible defaults. */
function changedFile(path: string): WorkspaceChangedFile {
  return {
    path,
    name: path.split("/").at(-1) ?? path,
    status: "modified",
    bytes: 10,
    modified_at: null,
  };
}

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
    expect(screen.getByText("src/app.ts")).toBeInTheDocument();
  });

  it("uses the no-workspace copy for the no_workspace reason", () => {
    renderList({ files: [], trackingComplete: false, trackingReason: "no_workspace" });

    expect(screen.getByText(/limited change tracking/i)).toBeInTheDocument();
    expect(screen.getByText(/no tracked workspace/i)).toBeInTheDocument();
  });

  it("shows the normal empty state (no notice) when tracking is complete", () => {
    // Git workspaces report complete tracking, so an empty list genuinely
    // means nothing changed — keep the plain empty state.
    renderList({ files: [], trackingComplete: true });

    expect(screen.getByText(/no workspace changes yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/limited change tracking/i)).not.toBeInTheDocument();
  });
});
