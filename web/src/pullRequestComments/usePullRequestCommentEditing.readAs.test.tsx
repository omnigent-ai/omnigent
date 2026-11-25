// Edit and Delete on the Pull Requests page while it reads as one of this Mac's logins: GitHub's
// can-edit and can-delete describe that reader, so they're offered only where it's also the
// account the change would be made as.

import type * as IdentityModule from "@/lib/identity";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
  subscribeIdentity: () => () => {},
}));

import { MacReadAsContext } from "@/hooks/macReadAs";
import type { PullRequestComment } from "@/newPullRequestPage/hooks/usePullRequests";
import {
  COMMENT_EDIT_WRITE_OPERATIONS,
  GITHUB_WRITE_SET_V1,
  resetDesktopPullRequestWritesForTests,
  setCommentEditsFlagForTests,
  setMacWritesReleaseFlagForTests,
} from "@/lib/desktopPullRequestWrites";
import {
  acceptMacAnswer,
  desktopReadTarget,
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  macReadTicket,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import type { MacNamedReader } from "@/lib/macNamedReads";
import type { ReviewThread } from "@/lib/pullRequestReviewThreads";
import type { ReviewThreadContext } from "@/newPullRequestPage/shell/PullRequestReviewThread";
import { resetMacNamedReadsForTests } from "@/lib/macNamedReads";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import {
  HOST_LOGIN,
  installNamedWriteShell,
  removeNamedWriteShell,
} from "@/test/macNamedWriteShell";

import { resetCommentChangeRetirementForTests } from "./commentChangeRetirement";
import { resetCommentEditsForTests } from "./commentEditStore";
import { useConversationCommentCard, useReviewCommentCard } from "./PullRequestCommentActions";

const PR = "https://github.com/acme/app/pull/42";
const ACTOR = "mac-octo";
const COMMENT: PullRequestComment = {
  id: "IC_1",
  author: "bob",
  body: "Before",
  created_at: "2026-11-05T10:00:00Z",
  url: `${PR}#issuecomment-1`,
  can_update: true,
  can_delete: true,
};
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
  diff_hunk: "@@ -1 +1 @@",
  comments: {
    data: [
      {
        id: "PRRC_1",
        author: "bob",
        body: "Review note",
        created_at: "2026-11-05T10:00:00Z",
        url: `${PR}#discussion_r1`,
        can_update: true,
        can_delete: true,
      },
    ],
    has_more: false,
    next_cursor: null,
  },
};
const CONTEXT: ReviewThreadContext = {
  conversationId: "pull-requests-page",
  prUrl: PR,
  account: ACTOR,
  canReply: true,
  canComment: true,
  loadComments: vi.fn(),
  loadingComments: null,
};

/** A shell reading and writing github.com as `mac-octo`, with comment edits on. */
async function installShell() {
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery: vi.fn(async () => ({ ok: true, data: [] })),
    codeHostWrite: vi.fn(async () => ({ ok: false, error: { code: "query_failed" } })),
    codeHostWriteCancel: vi.fn(),
    codeHostAccounts: {
      get: async () => ({
        ok: true,
        data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
      }),
      set: vi.fn(async () => ({ ok: false })),
      onChanged: () => () => {},
    },
    getDesktopFeatures: async () => ({
      codeHosts: [
        {
          id: "github",
          operations: [...GITHUB_MAC_READ_OPERATIONS, "pull_request_comment_state"],
          accounts: true,
          writes: {
            version: 1,
            operations: [...GITHUB_WRITE_SET_V1, ...COMMENT_EDIT_WRITE_OPERATIONS],
          },
        },
      ],
    }),
  };
  githubMacReadsPreference.write("on");
  await desktopReadTarget(PR);
  expect(
    acceptMacAnswer(macReadTicket("github", "github.com"), {
      identity: { host: "github.com", login: ACTOR, generation: 1 },
    }),
  ).toBe("accepted");
  setMacWritesReleaseFlagForTests(true);
  setCommentEditsFlagForTests(true);
}

/** Inside the Pull Requests page, reading as `page.reader` (null: Automatic). */
function pageWrapper(page: { reader: MacNamedReader | null }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <MacReadAsContext.Provider value={page}>{children}</MacReadAsContext.Provider>
    </QueryClientProvider>
  );
}

type Card = ReturnType<typeof useConversationCommentCard>;
const rowLabels = (card: Card) => card.actions.rows.map((row) => row.definition.menuLabel);

const renderCards = (reader: MacNamedReader | null) => {
  const wrapper = pageWrapper({ reader });
  const conversation = renderHook(() => useConversationCommentCard(COMMENT, CONTEXT, PR), {
    wrapper,
  });
  const review = renderHook(
    () => useReviewCommentCard(THREAD.comments.data[0]!, THREAD, CONTEXT, PR),
    { wrapper },
  );
  return { conversation: conversation.result.current, review: review.result.current };
};

beforeEach(() => {
  resetDesktopPullRequestWritesForTests();
  resetCommentEditsForTests();
  resetCommentChangeRetirementForTests();
});

afterEach(() => {
  usePaletteCommandsStore.setState({ groups: {} });
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetCommentEditsForTests();
  resetCommentChangeRetirementForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  setMacWritesReleaseFlagForTests(null);
  removeNamedWriteShell();
});

describe("Edit and Delete where the page reads as another login than the one that acts", () => {
  it("offers neither on conversation or review comments, whatever GitHub said of the reader", async () => {
    await installShell();
    const { conversation, review } = renderCards({ host: "github.com", login: "bob" });
    for (const card of [conversation, review]) {
      expect(rowLabels(card)).not.toContain("Edit");
      expect(rowLabels(card)).not.toContain("Delete…");
      expect(card.editing.actions).toBeFalsy();
    }
  });

  it("offers both where the page reads as the account that acts, on a shell writing by name", async () => {
    const shell = installNamedWriteShell(undefined, { extraWrites: COMMENT_EDIT_WRITE_OPERATIONS });
    await shell.ready(PR, { asBob: false });
    await shell.readNamed(PR, { host: "github.com", login: HOST_LOGIN });
    setCommentEditsFlagForTests(true);
    const { conversation, review } = renderCards({ host: "github.com", login: "ALICE" });
    for (const card of [conversation, review]) {
      expect(rowLabels(card)).toContain("Edit");
      expect(rowLabels(card)).toContain("Delete…");
    }
  });

  it("offers neither on an older shell, even where the page picked the host's own login", async () => {
    await installShell();
    const { conversation, review } = renderCards({ host: "github.com", login: "MAC-OCTO" });
    for (const card of [conversation, review]) {
      expect(rowLabels(card)).not.toContain("Edit");
      expect(rowLabels(card)).not.toContain("Delete…");
    }
  });

  it("offers both under Automatic, and for a reader on another host", async () => {
    await installShell();
    for (const reader of [null, { host: "ghe.example.com", login: "bob" }]) {
      const { conversation, review } = renderCards(reader);
      for (const card of [conversation, review]) expect(rowLabels(card)).toContain("Edit");
    }
  });
});
