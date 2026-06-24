import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import i18n from "@/i18n";
import { RunnerOfflineError } from "@/hooks/useWorkspaceChangedFiles";
import { FolderTree } from "./FolderTree";

const t = i18n.getFixedT(null, "common");

afterEach(cleanup);

/** Render FolderTree (the "All" files tab) with defaults, overriding per test. */
function renderTree(props: Partial<Parameters<typeof FolderTree>[0]> = {}) {
  return render(
    <FolderTree
      files={undefined}
      isLoading={false}
      isError={false}
      error={null}
      onFileSelect={vi.fn()}
      conversationId="conv_abc"
      showHidden={false}
      changedFiles={undefined}
      {...props}
    />,
  );
}

describe("FolderTree runner-offline state", () => {
  it("shows the reconnect hint when the runner went offline (session failed)", () => {
    // With runnerWentOffline the "All" tab shows the same reconnect hint as
    // the Changed tab, not the generic "Failed to load".
    renderTree({ isError: true, error: new RunnerOfflineError(), runnerWentOffline: true });

    expect(screen.getByText(t("agentAsleep"))).toBeInTheDocument();
    expect(screen.getByText(t("sendMessageToReconnect"))).toBeInTheDocument();
    expect(screen.queryByText(/failed to load/i)).not.toBeInTheDocument();
  });

  it("shows the empty state (not the asleep hint) for a new session that hasn't started", () => {
    // A new session 503s while connecting but never went "failed" — show
    // the normal empty state, not the asleep alarm.
    renderTree({ isError: true, error: new RunnerOfflineError(), runnerWentOffline: false });

    expect(screen.getByText(t("noFilesInWorkspace"))).toBeInTheDocument();
    expect(screen.queryByText(t("agentAsleep"))).not.toBeInTheDocument();
    expect(screen.queryByText(/failed to load/i)).not.toBeInTheDocument();
  });

  it("still shows the raw error for a non-runner-offline failure", () => {
    renderTree({ isError: true, error: new Error("500 Internal Server Error") });

    expect(screen.getByText(/failed to load: 500 internal server error/i)).toBeInTheDocument();
    expect(screen.queryByText(t("agentAsleep"))).not.toBeInTheDocument();
  });
});
