import { describe, expect, it, vi } from "vitest";

import {
  changeStateWithComment,
  type StateChainNotice,
  type StateChainSteps,
  stateChainMessage,
} from "./pullRequestStateChain";

function steps(overrides: Partial<StateChainSteps> = {}): StateChainSteps & {
  order: string[];
} {
  const order: string[] = [];
  return {
    order,
    withComment: true,
    post: vi.fn(async () => {
      order.push("post");
      return true;
    }),
    disposed: () => false,
    stillCurrent: () => true,
    change: vi.fn(async () => {
      order.push("change");
      return { kind: "changed" as const, state: "CLOSED" as const };
    }),
    ...overrides,
  };
}

describe("changeStateWithComment", () => {
  it("posts the comment, then changes the state, in that order", async () => {
    const chain = steps();
    expect(await changeStateWithComment(chain)).toEqual({ kind: "changed", state: "CLOSED" });
    expect(chain.order).toEqual(["post", "change"]);
  });

  it("changes the state alone without a comment", async () => {
    const chain = steps({ withComment: false });
    await changeStateWithComment(chain);
    expect(chain.post).not.toHaveBeenCalled();
    expect(chain.change).toHaveBeenCalledTimes(1);
  });

  it("never changes the state when the comment doesn't post", async () => {
    const chain = steps({ post: vi.fn(async () => false) });
    expect(await changeStateWithComment(chain)).toEqual({ kind: "comment-failed" });
    expect(chain.change).not.toHaveBeenCalled();
  });

  it("stops after the post once the box that started it is gone", async () => {
    let gone = false;
    const chain = steps({
      post: vi.fn(async () => {
        gone = true;
        return true;
      }),
      disposed: () => gone,
    });
    expect(await changeStateWithComment(chain)).toEqual({
      kind: "skipped",
      reason: "disposed",
      commentPosted: true,
    });
    expect(chain.change).not.toHaveBeenCalled();
  });

  it("stops after the post when what the click captured no longer holds", async () => {
    const chain = steps({ stillCurrent: () => false });
    expect(await changeStateWithComment(chain)).toEqual({
      kind: "skipped",
      reason: "changed",
      commentPosted: true,
    });
    expect(chain.change).not.toHaveBeenCalled();
  });

  it.each([
    [
      { kind: "refused", code: "rejected", message: "No." },
      { kind: "refused", message: "No.", commentPosted: true },
    ],
    [
      { kind: "unknown", message: "Maybe." },
      { kind: "unknown", commentPosted: true },
    ],
    [{ kind: "abandoned" }, { kind: "abandoned", commentPosted: true }],
    [
      { kind: "changed", state: "MERGED" },
      { kind: "changed", state: "MERGED" },
    ],
  ] as const)("reports a state change that came to %j", async (answer, expected) => {
    const chain = steps({ change: vi.fn(async () => answer) });
    expect(await changeStateWithComment(chain)).toEqual(expected);
  });
});

describe("stateChainMessage", () => {
  const boundTo = {
    authGeneration: 1,
    actor: { login: "mac-octo", generation: 3 },
    reader: null,
    read: { scope: "github", sequence: 1 },
    scope: ["github", "user-1", "github.com"] as never,
    host: { conversationId: "conv_1", key: "host-route" },
  };
  const notice = (outcome: StateChainNotice["outcome"]): StateChainNotice => ({
    operation: "pull_request_close",
    attempt: "attempt-test",
    outcome,
    boundTo,
  });

  it("says a refusal changed nothing, and that the comment was posted", () => {
    expect(
      stateChainMessage(
        notice({
          kind: "refused",
          message: "GitHub didn't accept this change.",
          commentPosted: true,
        }),
      ),
    ).toBe(
      "Your comment was posted. The pull request wasn't closed: GitHub didn't accept this change.",
    );
  });

  it("says an unknown outcome may have closed it, and not to retry before checking", () => {
    expect(stateChainMessage(notice({ kind: "unknown", commentPosted: false }))).toBe(
      "GitHub may have closed the pull request. Check it on GitHub before trying again.",
    );
  });

  it("says a skipped change was skipped because something changed", () => {
    expect(
      stateChainMessage({
        ...notice({ kind: "skipped", reason: "changed", commentPosted: true }),
        operation: "pull_request_reopen",
      }),
    ).toBe(
      "Your comment was posted. The pull request wasn't reopened, because the account or the pull request changed. Check it on GitHub.",
    );
  });
});
