// Check Again for a comment change made as the Pull Requests page's own login, which GitHub didn't
// confirm: it reads as that same login under its sign-in now, so it still settles after the login
// signed out and back in, and never as the host's account or another login.

import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
  subscribeIdentity: () => () => {},
}));

import {
  resetDesktopPullRequestWritesForTests,
  setMacWritesReleaseFlagForTests,
} from "@/lib/desktopPullRequestWrites";
import { resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import { resetMacNamedReadsForTests } from "@/lib/macNamedReads";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";
import { BOB, installNamedWriteShell, removeNamedWriteShell } from "@/test/macNamedWriteShell";

import { checkCommentChange, checkNeedsLogin } from "./commentChangeCheck";
import {
  commentEditKey,
  EMPTY_ENTRY,
  readCommentEdit,
  resetCommentEditsForTests,
  updateCommentEdit,
} from "./commentEditStore";

const PR = "https://github.com/acme/app/pull/42";
const COMMENT_ID = "IC_1";
const KEY = commentEditKey("user-1", PR, COMMENT_ID);
const SESSION = "pull-requests-page";

/** The comment as each login reads it: gone for Bob, whose delete GitHub didn't confirm. */
const commentState = (absentFor: string) => (login: string) => ({
  object: "session.github.pr_comment_state",
  comment_id: COMMENT_ID,
  ...(login === absentFor ? { state: "absent" } : { state: "present", body: "Still here" }),
});

/** A delete sent as Bob that GitHub may or may not have made. */
function holdUnconfirmedDelete() {
  updateCommentEdit(KEY, () => ({
    ...EMPTY_ENTRY,
    kind: "issue",
    unknown: {
      change: "delete",
      requestId: "req-1",
      check: {
        prUrl: PR,
        repository: "acme/app",
        number: 42,
        commentId: COMMENT_ID,
        kind: "issue",
        login: "bob",
        reader: BOB,
      },
    },
  }));
}

const stateReads = (shell: ReturnType<typeof installNamedWriteShell>) =>
  shell.codeHostQuery.mock.calls
    .map(([request]) => request)
    .filter((request) => request.operation === "pull_request_comment_state");

afterEach(() => {
  setMacWritesReleaseFlagForTests(null);
  resetCommentEditsForTests();
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  removeNamedWriteShell();
});

describe("Check Again for a change made as the page's own login", () => {
  it("waits for that login after it signs out, then settles under its new sign-in", async () => {
    const shell = installNamedWriteShell(() => ({ ok: false }), {
      reads: { pull_request_comment_state: commentState("bob") },
    });
    await shell.ready(PR);
    holdUnconfirmedDelete();

    // Bob signs out: nothing can check as him, and nothing checks as anyone else.
    shell.state.bobSignedIn = false;
    await expect(shell.readAsBob(PR)).rejects.toBeTruthy();
    expect(checkNeedsLogin(readCommentEdit(KEY))).toBe("bob");
    checkCommentChange(new QueryClient(), SESSION, KEY);
    expect(stateReads(shell)).toEqual([]);

    // Bob signs in again, with a new credential generation: Check Again reads as him now.
    shell.state.bobSignedIn = true;
    shell.state.bobGeneration = 3;
    await shell.readAsBob(PR);
    expect(checkNeedsLogin(readCommentEdit(KEY))).toBeNull();
    checkCommentChange(new QueryClient(), SESSION, KEY);
    await vi.waitFor(() => expect(readCommentEdit(KEY)).toBe(EMPTY_ENTRY));
    expect(stateReads(shell)).toHaveLength(1);
    expect(stateReads(shell)[0]).toMatchObject({ macLogin: "bob" });
    // Nothing was written again: a check never replays the change.
    expect(shell.codeHostWrite).not.toHaveBeenCalled();
    expect(shell.codeHostWriteAs).not.toHaveBeenCalled();
  });

  it("keeps it held when the login signs in again while the check reads", async () => {
    const shell = installNamedWriteShell(() => ({ ok: false }), {
      reads: { pull_request_comment_state: commentState("bob") },
    });
    await shell.ready(PR);
    holdUnconfirmedDelete();
    const read = shell.pauseNextRead("pull_request_comment_state");
    checkCommentChange(new QueryClient(), SESSION, KEY);
    await read.started;
    // The answer comes back under Bob's new sign-in, not the one the check began under.
    shell.bobSignsInAgain(1);
    read.release();
    await vi.waitFor(() => expect(readCommentEdit(KEY).checking).toBeFalsy());
    expect(readCommentEdit(KEY).unknown).not.toBeNull();
    // A fresh check under the new sign-in settles it, still without sending anything.
    checkCommentChange(new QueryClient(), SESSION, KEY);
    await vi.waitFor(() => expect(readCommentEdit(KEY)).toBe(EMPTY_ENTRY));
    expect(shell.codeHostWrite).not.toHaveBeenCalled();
    expect(shell.codeHostWriteAs).not.toHaveBeenCalled();
  });

  it("doesn't check as the host's account where the shell can't write as the login", async () => {
    const shell = installNamedWriteShell(() => ({ ok: false }), {
      writeAs: false,
      reads: { pull_request_comment_state: commentState("alice") },
    });
    await shell.ready(PR);
    holdUnconfirmedDelete();
    expect(checkNeedsLogin(readCommentEdit(KEY))).toBe("bob");
    checkCommentChange(new QueryClient(), SESSION, KEY);
    expect(stateReads(shell)).toEqual([]);
    expect(readCommentEdit(KEY).unknown).not.toBeNull();
  });
});
