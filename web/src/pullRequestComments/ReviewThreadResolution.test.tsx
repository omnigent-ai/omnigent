// Resolve and Unresolve on a review conversation, over a stand-in shell: the button by the reply
// box, the header's ⋯ and ⌘K all send the one write for that thread; a change on its way shows
// in every view of the thread; a thread or shell that can't make the change offers none of it.

import type * as SonnerModule from "sonner";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("sonner", async (importOriginal) => ({
  ...(await importOriginal<typeof SonnerModule>()),
  toast: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    dismiss: vi.fn(),
  }),
}));
vi.mock("@/lib/identity", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  getCurrentUserId: () => "user-1",
}));

import { TooltipProvider } from "@/components/ui/tooltip";
import {
  resetDesktopPullRequestWritesForTests,
  setPageThreadResolutionOperationsForTests,
  THREAD_RESOLUTION_WRITE_OPERATIONS,
} from "@/lib/desktopPullRequestWrites";
import { resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import { resetMacNamedReadsForTests } from "@/lib/macNamedReads";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";
import type { ReviewThread } from "@/lib/pullRequestReviewThreads";
import {
  PullRequestReviewThread,
  type ReviewThreadContext,
} from "@/newPullRequestPage/shell/PullRequestReviewThread";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import { installNamedWriteShell, removeNamedWriteShell } from "@/test/macNamedWriteShell";

import { ThreadEntry } from "./PullRequestCommentsEntries";
import { resetReviewThreadResolutionsForTests } from "./useReviewThreadResolution";

const PR = "https://github.com/acme/app/pull/42";
const THREAD: ReviewThread = {
  id: "PRRT_1",
  path: "src/app.js",
  side: "RIGHT",
  line: 3,
  start_line: null,
  subject_type: "LINE",
  resolved: false,
  outdated: false,
  can_reply: true,
  can_resolve: true,
  can_unresolve: true,
  diff_hunk: "@@ -1 +1 @@",
  comments: {
    data: [
      {
        id: "PRRC_1",
        author: "bob",
        body: "Rename this",
        created_at: "2026-11-05T10:00:00Z",
        url: `${PR}#discussion_r1`,
      },
    ],
    has_more: false,
    next_cursor: null,
  },
};
const CONTEXT: ReviewThreadContext = {
  conversationId: "conv_1",
  prUrl: PR,
  account: "alice",
  canReply: true,
  loadComments: vi.fn(),
  loadingComments: null,
};
const resolvedAnswer = (resolved: boolean) => ({
  ok: true,
  actor: { login: "alice" },
  data: {
    object: "session.github.pr_review_thread_resolution",
    thread_id: THREAD.id,
    resolved,
  },
});

let client: QueryClient;

/** A shell offering the resolution writes (unless `offered` is empty), answered in turn. */
async function ready(
  answers: unknown[],
  offered: readonly string[] = THREAD_RESOLUTION_WRITE_OPERATIONS,
) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const queue = [...answers];
  const shell = installNamedWriteShell(() => queue.shift() ?? { ok: false }, {
    extraWrites: offered,
  });
  await shell.ready(PR, { asBob: false });
  return shell;
}

const writes = (shell: Awaited<ReturnType<typeof ready>>) =>
  shell.codeHostWrite.mock.calls.map(([request]) => request as Record<string, unknown>);

function showDiffThread(thread: ReviewThread = THREAD) {
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <PullRequestReviewThread thread={thread} context={CONTEXT} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

function showBoth(thread: ReviewThread = THREAD) {
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <PullRequestReviewThread thread={thread} context={CONTEXT} />
        <ThreadEntry
          thread={thread}
          context={CONTEXT}
          host="github.com"
          pullRequestAuthor="author-octo"
          pullRequestUrl={PR}
          expanded
          onExpandedChange={vi.fn()}
          revealed={new Set()}
          onReveal={vi.fn()}
        />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

const paletteRows = () =>
  Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) => group.commands);
const diffThread = () => screen.getByRole("region", { name: /^Review conversation/ });

afterEach(() => {
  cleanup();
  client?.clear();
  vi.mocked(toast.error).mockClear();
  usePaletteCommandsStore.setState({ groups: {} });
  resetReviewThreadResolutionsForTests();
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  removeNamedWriteShell();
});

describe("resolving a review conversation", () => {
  it("sends the thread's resolve from the button by the reply box, and waits while it's on its way", async () => {
    let answer: (value: unknown) => void = () => {};
    const shell = await ready([]);
    shell.codeHostWrite.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    );
    showDiffThread();
    fireEvent.click(within(diffThread()).getByRole("button", { name: "Resolve Conversation" }));
    await waitFor(() => expect(writes(shell)).toHaveLength(1));
    expect(writes(shell)[0]).toMatchObject({
      operation: "pull_request_review_thread_resolve",
      params: { repository: "acme/app", number: 42, thread_id: THREAD.id },
    });
    const waiting = within(diffThread()).getByRole("button", { name: "Resolving…" });
    expect(waiting).toBeDisabled();
    fireEvent.click(waiting);
    await act(async () => answer(resolvedAnswer(true)));
    await waitFor(() =>
      expect(
        within(diffThread()).getByRole("button", { name: "Resolve Conversation" }),
      ).toBeEnabled(),
    );
    expect(writes(shell)).toHaveLength(1);
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("folds once the thread reads resolved, keeping focus on its header, and opens again on unresolve", async () => {
    await ready([resolvedAnswer(true), resolvedAnswer(false)]);
    const view = showDiffThread();
    fireEvent.click(within(diffThread()).getByRole("button", { name: "Resolve Conversation" }));
    await waitFor(() =>
      expect(
        within(diffThread()).getByRole("button", { name: "Resolve Conversation" }),
      ).toBeEnabled(),
    );
    // The read after the change says resolved.
    view.rerender(
      <QueryClientProvider client={client}>
        <TooltipProvider>
          <PullRequestReviewThread thread={{ ...THREAD, resolved: true }} context={CONTEXT} />
        </TooltipProvider>
      </QueryClientProvider>,
    );
    const toggle = within(diffThread()).getByRole("button", { name: /^New line 3/ });
    expect(toggle).toHaveFocus();
    expect(within(diffThread()).queryByText("Rename this")).toBeNull();

    fireEvent.pointerDown(
      within(diffThread()).getByRole("button", { name: "Conversation actions" }),
      {
        button: 0,
        ctrlKey: false,
      },
    );
    fireEvent.click(await screen.findByRole("menuitem", { name: "Unresolve Conversation" }));
    await waitFor(() =>
      expect(screen.queryByRole("menuitem", { name: "Unresolving…" })).toBeNull(),
    );
    view.rerender(
      <QueryClientProvider client={client}>
        <TooltipProvider>
          <PullRequestReviewThread thread={THREAD} context={CONTEXT} />
        </TooltipProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(within(diffThread()).getByText("Rename this")).toBeVisible());
  });

  it("offers Unresolve on a resolved thread, from its header's ⋯", async () => {
    const shell = await ready([resolvedAnswer(false)]);
    showDiffThread({ ...THREAD, resolved: true });
    const menu = within(diffThread()).getByRole("button", { name: "Conversation actions" });
    fireEvent.pointerDown(menu, { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Unresolve Conversation" }));
    await waitFor(() => expect(writes(shell)).toHaveLength(1));
    expect(writes(shell)[0]).toMatchObject({ operation: "pull_request_review_thread_unresolve" });
  });

  it("offers ⌘K's row only for the conversation worked in last, running the same write", async () => {
    const shell = await ready([resolvedAnswer(true)]);
    showDiffThread();
    expect(paletteRows().map((row) => row.label)).not.toContain("Resolve Conversation");
    fireEvent.focus(within(diffThread()).getByRole("button", { name: /^New line 3/ }));
    const row = await waitFor(() => {
      const found = paletteRows().find((command) => command.label === "Resolve Conversation");
      expect(found).toBeDefined();
      return found!;
    });
    act(() => row.run());
    await waitFor(() => expect(writes(shell)).toHaveLength(1));
    expect(writes(shell)[0]).toMatchObject({ params: { thread_id: THREAD.id } });
  });

  it("shows a change on its way in every view of the thread, and sends it once", async () => {
    const shell = await ready([]);
    shell.codeHostWrite.mockImplementationOnce(() => new Promise(() => {}));
    showBoth();
    const [first] = screen.getAllByRole("button", { name: "Resolve Conversation" });
    fireEvent.click(first!);
    await waitFor(() =>
      expect(screen.getAllByRole("button", { name: "Resolving…" })).toHaveLength(2),
    );
    for (const button of screen.getAllByRole("button", { name: "Resolving…" })) {
      expect(button).toBeDisabled();
    }
    expect(writes(shell)).toHaveLength(1);
  });

  it("tells a refusal, and says nothing when GitHub already has it that way", async () => {
    await ready([
      {
        ok: false,
        error: { code: "cannot_resolve", message: "You can't resolve this conversation." },
      },
      {
        ok: false,
        error: { code: "already_resolved", message: "This conversation is already resolved." },
      },
    ]);
    showDiffThread();
    const button = () => within(diffThread()).getByRole("button", { name: "Resolve Conversation" });
    fireEvent.click(button());
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("You can't resolve this conversation."),
    );
    await waitFor(() => expect(button()).toBeEnabled());
    fireEvent.click(button());
    await waitFor(() => expect(button()).toBeEnabled());
    expect(toast.error).toHaveBeenCalledTimes(1);
  });
});

describe("where a conversation can't be resolved here", () => {
  const offersNothing = () => {
    expect(screen.queryByRole("button", { name: "Resolve Conversation" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Conversation actions" })).toBeNull();
  };

  it("offers nothing when GitHub doesn't let the viewer", async () => {
    await ready([]);
    showBoth({ ...THREAD, can_resolve: false });
    offersNothing();
  });

  it("offers nothing on a read from before the permission was read", async () => {
    await ready([]);
    const { can_resolve: _resolve, can_unresolve: _unresolve, ...older } = THREAD;
    showBoth(older);
    offersNothing();
  });

  it("offers nothing on a shell that doesn't list the writes", async () => {
    await ready([], []);
    showBoth();
    offersNothing();
  });

  it("offers nothing from a bundle that doesn't implement them, whatever the shell lists", async () => {
    await ready([]);
    setPageThreadResolutionOperationsForTests([]);
    showBoth();
    offersNothing();
  });
});
