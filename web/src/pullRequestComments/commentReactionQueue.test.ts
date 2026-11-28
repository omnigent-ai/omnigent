// Reaction queues over a stand-in environment: one PR's clicks go one at a time as captured,
// every view of a scope shares one queue, a refused or unconfirmed click rereads its comment and
// shows what GitHub has, a failed reread says so inline, and once a scope is gone its queue is
// disposed and nothing it started acts again, even when the same scope returns.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CommentReaction, CommentReactionsState } from "@/lib/pullRequestCommentReactions";

import {
  type ReactionIntentEffects,
  type ReactionScope,
  type ReactionSendOutcome,
  reactionQueueFor,
  resetReactionQueuesForTests,
  setReactionEnvironment,
} from "./commentReactionQueue";

const PR = "https://github.com/acme/app/pull/42";
const HEART_BY_B: CommentReaction = {
  content: "HEART",
  count: 1,
  viewer_reacted: false,
  logins: ["b"],
};
const EYES_MINE: CommentReaction = {
  content: "EYES",
  count: 1,
  viewer_reacted: true,
  logins: ["me"],
};

const scopeAs = (login: string, owner = "user-1"): ReactionScope => ({
  prUrl: PR,
  owner,
  authGeneration: 1,
  actor: { login, generation: 1 },
  host: { conversationId: "session-1", key: "server:session-1" },
});

// Which scopes act now; a test switches account or user by changing these.
const world = { login: "me", owner: "user-1" };
let retire: () => void = () => {};

beforeEach(() => {
  world.login = "me";
  world.owner = "user-1";
  setReactionEnvironment({
    scopeCurrent: (scope) => scope.owner === world.owner && scope.actor.login === world.login,
    subscribe: (listener) => {
      retire = listener;
      return () => {
        retire = () => {};
      };
    },
  });
});
afterEach(() => resetReactionQueuesForTests());

/** Effects whose sends and rereads wait for the test to settle them, in order. */
function stand(scope: ReactionScope = scopeAs("me")) {
  const queue = reactionQueueFor(scope);
  const sent: { content: string; add: boolean; settle: (outcome: ReactionSendOutcome) => void }[] =
    [];
  const state = { reread: null as CommentReactionsState | null, refreshes: 0 };
  let resolveRefresh: () => void = () => {};
  const reread = vi.fn(async () => state.reread);
  const effects = (content: string, add: boolean): ReactionIntentEffects => ({
    send: (settle) => sent.push({ content, add, settle }),
    reread,
    refresh: () => {
      state.refreshes += 1;
      return new Promise<void>((resolve) => {
        resolveRefresh = resolve;
      });
    },
  });
  const click = (content: "HEART" | "EYES" | "THUMBS_UP", add: boolean, commentId = "IC_1") =>
    queue.enqueue({ commentId, content, add }, effects(content, add));
  const shown = (base: CommentReaction[] = [HEART_BY_B], commentId = "IC_1") =>
    queue.displayed(commentId, base);
  return { queue, sent, state, reread, click, shown, refreshed: () => resolveRefresh() };
}

const flush = () =>
  new Promise((resolve) => {
    setTimeout(resolve, 0);
  });

describe("a pull request's reaction clicks", () => {
  it("send one at a time, in order, each as captured", () => {
    const { sent, click, shown } = stand();
    click("HEART", true);
    click("HEART", false);
    click("HEART", true);
    expect(sent.map(({ add }) => add)).toEqual([true]);
    expect(shown().reactions).toEqual([
      { ...HEART_BY_B, count: 2, viewer_reacted: true, logins: ["me", "b"] },
    ]);
    expect([...shown().pending]).toEqual(["HEART"]);
    sent[0]!.settle({ kind: "made" });
    expect(sent.map(({ add }) => add)).toEqual([true, false]);
    sent[1]!.settle({ kind: "made" });
    sent[2]!.settle({ kind: "made" });
    expect(sent.map(({ add }) => add)).toEqual([true, false, true]);
  });

  it("share one queue across every view of a scope, however the PR's link is written", () => {
    const { queue, click } = stand();
    click("EYES", true);
    const other = reactionQueueFor({ ...scopeAs("me"), prUrl: `${PR.toUpperCase()}/` });
    expect(other).toBe(queue);
    expect(other.displayed("IC_1", []).reactions.map(({ content }) => content)).toEqual(["EYES"]);
    // Another workspace user's queue for the PR is another queue, with none of them.
    expect(reactionQueueFor(scopeAs("me", "user-2")).displayed("IC_1", []).reactions).toEqual([]);
  });

  it("show GitHub's answer once made, then let the PR's next read say", async () => {
    const { sent, state, click, shown, refreshed } = stand();
    click("EYES", true);
    sent[0]!.settle({ kind: "made", reactions: [HEART_BY_B, EYES_MINE] });
    expect(state.refreshes).toBe(1);
    expect(shown([]).reactions).toEqual([HEART_BY_B, EYES_MINE]);
    expect(shown([]).pending.size).toBe(0);
    refreshed();
    await flush();
    expect(shown([]).reactions).toEqual([]);
  });

  it("apply clicks not yet sent on top of GitHub's latest answer", () => {
    const { sent, click, shown } = stand();
    click("EYES", true);
    click("EYES", false);
    sent[0]!.settle({ kind: "made", reactions: [EYES_MINE] });
    expect(shown([]).reactions).toEqual([]);
    expect([...shown([]).pending]).toEqual(["EYES"]);
  });

  it("read the PR again only once nothing is left to send", () => {
    const { sent, state, click } = stand();
    click("EYES", true);
    click("HEART", true);
    sent[0]!.settle({ kind: "made" });
    expect(state.refreshes).toBe(0);
    sent[1]!.settle({ kind: "made" });
    expect(state.refreshes).toBe(1);
  });

  it("drop every click for a comment that was deleted", () => {
    const { sent, click, shown } = stand();
    click("EYES", true);
    click("HEART", true);
    click("HEART", true, "IC_2");
    sent[0]!.settle({ kind: "deleted" });
    expect(sent.map(({ content }) => content)).toEqual(["EYES", "HEART"]);
    expect(shown([]).reactions).toEqual([]);
  });
});

describe("a refused or unconfirmed click", () => {
  for (const outcome of [{ kind: "refused" }, { kind: "unknown" }] as const) {
    it(`(${outcome.kind}) rereads its comment and shows what GitHub has, holding the queue meanwhile`, async () => {
      const { sent, state, reread, click, shown } = stand();
      click("EYES", true);
      click("HEART", true);
      state.reread = { state: "present", canReact: true, reactions: [EYES_MINE] };
      sent[0]!.settle(outcome);
      expect(reread).toHaveBeenCalledTimes(1);
      expect(shown([]).rereading).toBe(true);
      expect(sent).toHaveLength(1);
      await flush();
      expect(shown([]).rereading).toBe(false);
      expect(shown([]).unconfirmed).toBe(false);
      // GitHub's reactions, with the heart still on its way on top.
      expect(shown([]).reactions.map(({ content }) => content)).toEqual(["HEART", "EYES"]);
      expect(sent).toHaveLength(2);
    });
  }

  it("shows the comment as GitHub has it when the click didn't land", async () => {
    const { sent, state, click, shown } = stand();
    click("EYES", true);
    state.reread = { state: "present", canReact: true, reactions: [HEART_BY_B] };
    sent[0]!.settle({ kind: "unknown" });
    await flush();
    expect(shown([]).reactions).toEqual([HEART_BY_B]);
    expect(sent).toHaveLength(1);
  });

  it("says once, inline, that it couldn't read the comment, and drops that comment's later clicks", async () => {
    const { sent, click, shown } = stand();
    click("EYES", true);
    click("HEART", true);
    click("HEART", true, "IC_2");
    sent[0]!.settle({ kind: "unknown" });
    await flush();
    expect(shown().unconfirmed).toBe(true);
    expect(shown().reactions).toEqual([HEART_BY_B]);
    // The other comment's click still goes.
    expect(sent.map(({ content }) => content)).toEqual(["EYES", "HEART"]);
    click("THUMBS_UP", true);
    expect(shown().unconfirmed).toBe(false);
  });

  it("settles as deleted when the comment is gone", async () => {
    const { sent, state, click, shown } = stand();
    click("EYES", true);
    state.reread = { state: "absent" };
    sent[0]!.settle({ kind: "unknown" });
    await flush();
    expect(shown([]).reactions).toEqual([]);
    expect(shown([]).unconfirmed).toBe(false);
  });
});

describe("a queue whose scope is gone", () => {
  it("is disposed by the environment's change, whether or not a view shows it", () => {
    const { queue, sent, click } = stand();
    click("EYES", true);
    click("HEART", true);
    world.login = "bob";
    retire();
    expect(queue.disposed).toBe(true);
    // The answer to the one already sent no longer counts.
    sent[0]!.settle({ kind: "made" });
    expect(sent).toHaveLength(1);
  });

  it("returns as a new queue with none of the old one's clicks, when the scope comes back", () => {
    const { queue, sent, click } = stand();
    click("EYES", true);
    click("HEART", true);
    world.login = "bob";
    retire();
    world.login = "me";
    retire();
    const fresh = reactionQueueFor(scopeAs("me"));
    expect(fresh).not.toBe(queue);
    expect(fresh.displayed("IC_1", []).reactions).toEqual([]);
    sent[0]!.settle({ kind: "made" });
    expect(sent).toHaveLength(1);
  });

  it("ignores a late answer that would have rerun or shown anything", async () => {
    const { queue, sent, state, reread, click } = stand();
    click("EYES", true);
    world.login = "bob";
    retire();
    sent[0]!.settle({ kind: "unknown" });
    sent[0]!.settle({ kind: "refused" });
    await flush();
    expect(reread).not.toHaveBeenCalled();
    expect(state.refreshes).toBe(0);
    expect(queue.displayed("IC_1", []).reactions).toEqual([]);
  });

  it("ignores a reread that answers after the switch", async () => {
    const { queue, sent, state, click } = stand();
    click("EYES", true);
    state.reread = { state: "present", canReact: true, reactions: [EYES_MINE] };
    sent[0]!.settle({ kind: "unknown" });
    world.login = "bob";
    retire();
    await flush();
    expect(state.refreshes).toBe(0);
    expect(queue.displayed("IC_1", []).reactions).toEqual([]);
  });

  it("refuses new clicks once disposed", () => {
    const { queue, sent, click } = stand();
    world.owner = "user-2";
    retire();
    expect(queue.disposed).toBe(true);
    click("EYES", true);
    expect(sent).toHaveLength(0);
  });

  it("ends when the workspace user changes before an answer", () => {
    const { queue, sent, click, shown } = stand();
    click("EYES", true);
    click("HEART", true);
    sent[0]!.settle({ kind: "abandoned" });
    expect(queue.disposed).toBe(true);
    expect(sent).toHaveLength(1);
    expect(shown().reactions).toEqual([HEART_BY_B]);
  });

  it("ends when its last view switches to another scope, and lives on when it only unmounted", () => {
    const switched = stand();
    switched.queue.hold();
    switched.click("EYES", true);
    switched.queue.release(true);
    expect(switched.queue.disposed).toBe(true);
    resetReactionQueuesForTests();
    const unmounted = stand();
    unmounted.queue.hold();
    unmounted.click("EYES", true);
    unmounted.queue.release(false);
    expect(unmounted.queue.disposed).toBe(false);
    unmounted.sent[0]!.settle({ kind: "made" });
    expect(unmounted.state.refreshes).toBe(1);
    unmounted.refreshed();
    return flush().then(() => expect(unmounted.queue.disposed).toBe(true));
  });
});
