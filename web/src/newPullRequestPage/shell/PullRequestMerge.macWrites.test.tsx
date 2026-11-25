// Merging and turning off auto-merge on the Mac write path, rendered with the real hooks
// and stores over a stand-in shell: the dialog only picks the method, main's own sheet
// confirms, and merge-queue repositories are left to GitHub.

import type * as IdentityModule from "@/lib/identity";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { currentUser, hostFetch, identityListeners } = vi.hoisted(() => ({
  currentUser: { id: "user-1" as string | null },
  hostFetch: vi.fn(),
  identityListeners: new Set<() => void>(),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => currentUser.id,
  subscribeIdentity: (listener: () => void) => {
    identityListeners.add(listener);
    return () => identityListeners.delete(listener);
  },
  authenticatedFetch: hostFetch,
}));

import type { PullRequest } from "@/newPullRequestPage/hooks/usePullRequests";
import {
  GITHUB_WRITE_SET_V1,
  MERGE_QUEUE_WRITE_OPERATIONS,
  resetDesktopPullRequestWritesForTests,
  setMergeQueueFlagForTests,
  setPageWriteOperationsForTests,
  writeMacWritesIntent,
} from "@/lib/desktopPullRequestWrites";
import { resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import { installMacWriteShell, removeMacWriteShell } from "@/test/macWriteShell";
import { PullRequestMerge, type MergeReadyAction } from "./PullRequestMerge";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const PR = "https://github.com/acme/app/pull/42";
const SESSION = "session-1";
const HEAD = "a".repeat(40);
const OPTIONS = {
  object: "session.github.merge_options",
  methods: ["merge", "squash"],
  default: "squash",
  merge_queue: false,
  merge_state_status: "CLEAN",
  auto_merge: null,
};
const OPEN_PR = {
  url: PR,
  number: 42,
  title: "Add the thing",
  state: "OPEN",
  base_ref: "main",
  head_sha: HEAD,
  merge_state_status: "CLEAN",
  auto_merge: null,
} as unknown as PullRequest;
const merged = {
  ok: true,
  data: { object: "session.github.pr_merge", pr_url: PR },
  actor: { login: "mac-octo" },
};

/** A page's ready caption, naming what the button would do. */
const readyCaption: Record<MergeReadyAction, string> = {
  merge: "Ready caption: merge",
  queue: "Ready caption: queue",
};

function renderMerge(pr: PullRequest = OPEN_PR, { caption = false }: { caption?: boolean } = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return render(
    <PullRequestMerge
      conversationId={SESSION}
      prUrl={PR}
      pr={pr}
      readyCaption={caption ? readyCaption : undefined}
    />,
    { wrapper },
  );
}

async function macShell(
  codeHostWrite: (request: unknown) => Promise<unknown>,
  options: Record<string, unknown> = OPTIONS,
) {
  const shell = await installMacWriteShell(PR, codeHostWrite, {
    pull_request_merge_options: options,
  });
  setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
  return shell;
}

async function openDialog() {
  fireEvent.click(await screen.findByRole("button", { name: "Squash and merge" }));
  return screen.findByRole("dialog");
}

beforeEach(() => {
  currentUser.id = "user-1";
  identityListeners.clear();
  resetDesktopPullRequestWritesForTests();
  hostFetch.mockReset();
});

afterEach(() => {
  writeMacWritesIntent("automatic");
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  removeMacWriteShell();
});

describe("merging on this Mac", () => {
  it("picks the method in the dialog, names the account, and leaves confirming to main's sheet", async () => {
    const { write, query } = await macShell(async () => merged);
    renderMerge();
    const dialog = await openDialog();
    expect(dialog).toHaveTextContent("It merges as @mac-octo; you’ll confirm in the next step.");
    expect(dialog).not.toHaveTextContent("This can’t be undone.");
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(write.mock.calls[0][0]).toMatchObject({
      operation: "pull_request_merge",
      params: { repository: "acme/app", number: 42, merge_method: "squash", expected_head: HEAD },
    });
    // The write, its answer and the PR's refresh run in turn; give a loaded machine time.
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull(), { timeout: 5000 });
    expect(query).toHaveBeenCalledWith(
      expect.objectContaining({ account: "mac", operation: "pull_request_merge_options" }),
    );
    expect(hostFetch).not.toHaveBeenCalled();
  });

  it("stays open without an error when main's sheet is cancelled", async () => {
    await macShell(async () => ({
      ok: false,
      error: {
        code: "cancelled",
        message: "The change was cancelled before it was sent. Nothing changed on GitHub.",
      },
    }));
    renderMerge();
    await openDialog();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled());
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("shows main's copy when the branch moved", async () => {
    await macShell(async () => ({
      ok: false,
      error: {
        code: "head_changed",
        message: "The pull request's branch changed. Review the new commits, then merge again.",
      },
    }));
    renderMerge();
    await openDialog();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The pull request's branch changed. Review the new commits, then merge again.",
    );
  });
});

describe("the host path", () => {
  it("keeps the dialog's own confirmation and sends the merge to the host", async () => {
    await installMacWriteShell(PR, async () => ({}));
    // The user keeps every change on the session's machine.
    writeMacWritesIntent("off");
    hostFetch.mockImplementation(async (url: string) =>
      String(url).includes("merge-options")
        ? new Response(JSON.stringify({ ...OPTIONS, merge_queue: undefined }), { status: 200 })
        : new Response(JSON.stringify({ pr_url: PR }), { status: 200 }),
    );
    renderMerge();
    const dialog = await openDialog();
    expect(dialog).toHaveTextContent("This can’t be undone.");
    fireEvent.click(within(dialog).getByRole("button", { name: "Squash and merge" }));
    await waitFor(() =>
      expect(hostFetch.mock.calls.some(([url]) => String(url).endsWith("/merge"))).toBe(true),
    );
  });
});

describe("merge queues on this Mac", () => {
  const QUEUE_OPTIONS = {
    ...OPTIONS,
    merge_queue: true,
    merge_queue_membership: "not_queued",
    merge_queue_method: "squash",
  };
  const queuedAnswer = (queued: boolean) => ({
    ok: true,
    data: { object: "session.github.pr_merge_queue", pr_url: PR, queued },
    actor: { login: "mac-octo" },
  });
  const unknownAnswer = {
    ok: false,
    error: { code: "outcome_unknown", message: "GitHub may have made this change." },
  };

  async function queueShell(
    codeHostWrite: (request: unknown) => Promise<unknown>,
    options: Record<string, unknown> = QUEUE_OPTIONS,
    extraWrites: readonly string[] = MERGE_QUEUE_WRITE_OPERATIONS,
  ) {
    const shell = await installMacWriteShell(
      PR,
      codeHostWrite,
      { pull_request_merge_options: options },
      { extraWrites },
    );
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    setMergeQueueFlagForTests(true);
    return shell;
  }

  it("adds the PR to the queue at the head it showed, naming the account, after main's sheet", async () => {
    const { write } = await queueShell(async () => queuedAnswer(true));
    renderMerge();
    fireEvent.click(await screen.findByRole("button", { name: "Add to merge queue" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Add pull request #42 to the merge queue?");
    expect(dialog).toHaveTextContent("with squash once its checks pass.");
    expect(dialog).toHaveTextContent("It’s added as @mac-octo; you’ll confirm in the next step.");
    // The queue's own method: nothing to pick.
    expect(within(dialog).queryByRole("combobox")).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "Continue" }));
    expect(write.mock.calls[0][0]).toMatchObject({
      operation: "pull_request_merge_queue_add",
      params: { repository: "acme/app", number: 42, expected_head: HEAD },
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull(), { timeout: 5000 });
    expect(hostFetch).not.toHaveBeenCalled();
  });

  async function addUnconfirmed({ caption = false }: { caption?: boolean } = {}) {
    const shell = await queueShell(async () => unknownAnswer);
    renderMerge(OPEN_PR, { caption });
    fireEvent.click(await screen.findByRole("button", { name: "Add to merge queue" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Continue" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The app couldn’t tell whether GitHub added this pull request to the merge queue. Check it before trying again.",
    );
    return shell;
  }

  it("asks to check when GitHub may have added it, and offers the queue again only after a fresh read", async () => {
    const { write } = await addUnconfirmed();
    expect(screen.queryByRole("button", { name: "Continue" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add to merge queue" })).toBeNull();
    expect(screen.getByRole("link", { name: "Open on GitHub" })).toHaveAttribute("href", PR);
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    // A fresh read says it isn't queued: the open dialog offers it again, by choice.
    expect(await screen.findByRole("button", { name: "Continue" })).toBeEnabled();
    expect(write).toHaveBeenCalledOnce();
  });
});
