import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { usePullRequestInfo, usePullRequestReadPriority } from "@/hooks/usePullRequests";
import { type PullRequestPanelDetails, refreshSessionPullRequests } from "@/shell/PullRequestPanel";

import { PullRequestPageBody, pullRequestPageTab } from "./PullRequestPageBody";
import { useStoredPullRequestFallback } from "./useStoredPullRequestFallback";

const captured = vi.hoisted(() => ({
  details: null as ComponentProps<typeof PullRequestPanelDetails> | null,
}));

vi.mock("@/hooks/usePullRequests", () => ({
  usePullRequestInfo: vi.fn(() => ({ data: undefined })),
  usePullRequestReadPriority: vi.fn(),
}));
vi.mock("@/shell/PullRequestPanel", () => ({
  PullRequestPanelDetails: (props: ComponentProps<typeof PullRequestPanelDetails>) => {
    captured.details = props;
    return <div data-testid="details">{props.refreshButton as ReactNode}</div>;
  },
  refreshSessionPullRequests: vi.fn(),
}));
vi.mock("./useStoredPullRequestFallback", () => ({
  useStoredPullRequestFallback: vi.fn(() => ({ notSynced: false })),
}));

const PR = "https://github.com/example-org/widgets/pull/12";

function renderBody(props: Partial<ComponentProps<typeof PullRequestPageBody>> = {}) {
  const client = new QueryClient();
  const onSectionChange = vi.fn();
  const onShowAllPullRequests = vi.fn();
  render(
    <QueryClientProvider client={client}>
      <PullRequestPageBody
        conversationId="conv_1"
        prUrl={PR}
        section="comments"
        active
        onSectionChange={onSectionChange}
        onShowAllPullRequests={onShowAllPullRequests}
        {...props}
      />
    </QueryClientProvider>,
  );
  return { client, onSectionChange, onShowAllPullRequests };
}

beforeEach(() => {
  captured.details = null;
  vi.mocked(usePullRequestInfo).mockClear();
  vi.mocked(refreshSessionPullRequests).mockReset();
  vi.mocked(useStoredPullRequestFallback).mockClear();
});
afterEach(cleanup);

describe("pullRequestPageTab", () => {
  it("shows Commits in the Changes section, and every other section as itself", () => {
    expect(pullRequestPageTab("commits")).toBe("changes");
    expect(pullRequestPageTab("comments")).toBe("comments");
    expect(pullRequestPageTab("changes")).toBe("changes");
  });
});

describe("PullRequestPageBody", () => {
  it("reads this pull request, polling only while its tab is on screen", () => {
    renderBody({ active: false });
    expect(usePullRequestInfo).toHaveBeenLastCalledWith("conv_1", { poll: false, prUrl: PR });
    expect(usePullRequestReadPriority).toHaveBeenLastCalledWith("conv_1", undefined);
    cleanup();
    renderBody();
    expect(usePullRequestInfo).toHaveBeenLastCalledWith("conv_1", { poll: true, prUrl: PR });
    // On screen, it reads ahead of queued background reads.
    expect(usePullRequestReadPriority).toHaveBeenLastCalledWith("conv_1", PR);
    expect(captured.details).toMatchObject({ conversationId: "conv_1", showHeading: false });
  });

  it("shows this pull request as the server stored it while the host is offline", () => {
    const stored = {
      url: PR,
      host: "github.com",
      repository: "example-org/widgets",
      number: 12,
      title: "Ship it",
      relationship: "created" as const,
    };
    vi.mocked(useStoredPullRequestFallback).mockReturnValue({ storedPr: stored, notSynced: false });
    renderBody();
    expect(vi.mocked(useStoredPullRequestFallback).mock.lastCall?.[2]).toBe(PR);
    expect(captured.details).toMatchObject({ storedPr: stored, notSynced: false });

    cleanup();
    vi.mocked(useStoredPullRequestFallback).mockReturnValue({ notSynced: true });
    renderBody();
    expect(captured.details).toMatchObject({ storedPr: undefined, notSynced: true });
  });

  it("shows the place's section, and reports only a section that actually changes", () => {
    const { onSectionChange } = renderBody({ section: "commits" });
    expect(captured.details?.activeTab).toBe("changes");
    act(() => captured.details!.setActiveTab("changes"));
    expect(onSectionChange).not.toHaveBeenCalled();
    act(() => captured.details!.setActiveTab("comments"));
    expect(onSectionChange).toHaveBeenCalledWith("comments");
  });

  it("shows the session's list of pull requests from its All pull requests button", async () => {
    const { onShowAllPullRequests } = renderBody();
    await userEvent.click(screen.getByRole("button", { name: "All pull requests" }));
    expect(onShowAllPullRequests).toHaveBeenCalledOnce();
  });

  it("refreshes the session's pull requests, disabled until the refresh settles", async () => {
    let finish!: () => void;
    vi.mocked(refreshSessionPullRequests).mockReturnValue(
      new Promise<void>((resolve) => {
        finish = resolve;
      }),
    );
    const { client } = renderBody();
    const refresh = screen.getByRole("button", { name: "Refresh" });
    await userEvent.click(refresh);
    expect(refreshSessionPullRequests).toHaveBeenCalledWith(client, "conv_1");
    expect(refresh).toBeDisabled();
    await act(async () => finish());
    expect(refresh).toBeEnabled();
  });
});
