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
