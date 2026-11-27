// A comment's reactions as the page reads, shows and changes them: only well-formed reads show
// any, a pending click applies once whichever way it goes, and a tooltip names who reacted.

import { describe, expect, it } from "vitest";

import {
  type CommentReaction,
  commentReactionsOf,
  parseCommentReactionsAnswer,
  parseReactionList,
  reactorsSummary,
  withViewerReaction,
} from "./pullRequestCommentReactions";

const reaction = (
  content: CommentReaction["content"],
  count: number,
  viewerReacted = false,
  logins: string[] = [],
): CommentReaction => ({ content, count, viewer_reacted: viewerReacted, logins });

describe("commentReactionsOf", () => {
  it("reads the reactions and whether the viewer may react, in picker order", () => {
    expect(
      commentReactionsOf({
        can_react: true,
        reactions: [reaction("EYES", 1), reaction("THUMBS_UP", 2, true, ["me", "b"])],
      }),
    ).toEqual({
      canReact: true,
      reactions: [reaction("THUMBS_UP", 2, true, ["me", "b"]), reaction("EYES", 1)],
    });
  });

  it("shows nothing for a read without them, as the host's and an older shell's are", () => {
    expect(commentReactionsOf({})).toBeNull();
    expect(commentReactionsOf({ reactions: [] })).toBeNull();
    expect(commentReactionsOf({ can_react: true })).toBeNull();
  });

  it("shows nothing for a malformed read", () => {
    for (const reactions of [
      [reaction("THUMBS_UP", 1), reaction("THUMBS_UP", 2)],
      [{ ...reaction("HEART", 1), content: "SPARKLES" }],
      [reaction("HEART", 0)],
      [{ ...reaction("HEART", 1), viewer_reacted: "yes" }],
      [{ ...reaction("HEART", 1), logins: [1] }],
      "HEART",
    ]) {
      expect(commentReactionsOf({ can_react: true, reactions })).toBeNull();
    }
  });

  it("keeps at most ten named reactors", () => {
    const logins = Array.from({ length: 12 }, (_, index) => `user${index}`);
    expect(parseReactionList([reaction("ROCKET", 12, false, logins)])![0]!.logins).toHaveLength(10);
  });
});

describe("withViewerReaction", () => {
  it("adds the viewer's reaction to one others left, naming them first", () => {
    expect(withViewerReaction([reaction("HEART", 1, false, ["b"])], "HEART", true, "me")).toEqual([
      reaction("HEART", 2, true, ["me", "b"]),
    ]);
  });

  it("adds a new reaction in picker order", () => {
    expect(
      withViewerReaction([reaction("EYES", 1), reaction("THUMBS_UP", 1)], "LAUGH", true, "me").map(
        (each) => each.content,
      ),
    ).toEqual(["THUMBS_UP", "LAUGH", "EYES"]);
  });

  it("removes the viewer's reaction, and the pill once nobody's left", () => {
    expect(
      withViewerReaction([reaction("HEART", 2, true, ["me", "b"])], "HEART", false, "me"),
    ).toEqual([reaction("HEART", 1, false, ["b"])]);
    expect(withViewerReaction([reaction("HEART", 1, true, ["me"])], "HEART", false, "me")).toEqual(
      [],
    );
  });

  it("names the viewer only as You where its login isn't known", () => {
    const [added] = withViewerReaction([], "ROCKET", true, "");
    expect(added).toEqual(reaction("ROCKET", 1, true, []));
    expect(reactorsSummary(added!, null)).toBe("You reacted with Rocket");
    const [joined] = withViewerReaction([reaction("ROCKET", 1, false, ["b"])], "ROCKET", true, "");
    expect(reactorsSummary(joined!, null)).toBe("You and @b reacted with Rocket");
    // GitHub's list may name the viewer too; the tooltip never names more people than reacted.
    expect(reactorsSummary(reaction("ROCKET", 2, true, ["me", "b"]), null)).toBe(
      "You and @me reacted with Rocket",
    );
  });

  it("leaves a reaction already as asked alone, so a settled click applies once", () => {
    const mine = [reaction("HEART", 1, true, ["me"])];
    expect(withViewerReaction(mine, "HEART", true, "me")).toEqual(mine);
    expect(withViewerReaction([], "HEART", false, "me")).toEqual([]);
  });
});

describe("reactorsSummary", () => {
  it("names the viewer as You, first", () => {
    expect(reactorsSummary(reaction("HEART", 2, true, ["b", "me"]), "me")).toBe(
      "You and @b reacted with Heart",
    );
  });

  it("names up to ten, then counts the rest", () => {
    const logins = Array.from({ length: 10 }, (_, index) => `u${index}`);
    expect(reactorsSummary(reaction("ROCKET", 13, false, logins), null)).toBe(
      `${logins.map((login) => `@${login}`).join(", ")} and 3 others reacted with Rocket`,
    );
    expect(reactorsSummary(reaction("EYES", 3, false, ["a", "b"]), null)).toBe(
      "@a, @b and 1 other reacted with Eyes",
    );
  });

  it("counts people GitHub didn't name", () => {
    expect(reactorsSummary(reaction("LAUGH", 1), null)).toBe("1 person reacted with Laugh");
    expect(reactorsSummary(reaction("LAUGH", 4), null)).toBe("4 people reacted with Laugh");
  });
});

describe("parseCommentReactionsAnswer", () => {
  const answer = (fields: Record<string, unknown>) => ({
    object: "session.github.pr_comment_reactions",
    comment_id: "IC_1",
    ...fields,
  });

  it("reads present, absent and unreadable answers for the comment asked about", () => {
    expect(
      parseCommentReactionsAnswer(
        answer({ state: "present", reactions: [reaction("EYES", 1)], can_react: true }),
        "IC_1",
      ),
    ).toEqual({ state: "present", reactions: [reaction("EYES", 1)], canReact: true });
    expect(parseCommentReactionsAnswer(answer({ state: "absent" }), "IC_1")).toEqual({
      state: "absent",
    });
    expect(parseCommentReactionsAnswer(answer({ state: "unreadable" }), "IC_1")).toEqual({
      state: "unreadable",
    });
  });

  it("refuses an answer about another comment, or a malformed one", () => {
    expect(parseCommentReactionsAnswer(answer({ state: "absent" }), "IC_2")).toBeNull();
    expect(parseCommentReactionsAnswer({ state: "absent", comment_id: "IC_1" }, "IC_1")).toBeNull();
    expect(
      parseCommentReactionsAnswer(
        answer({ state: "present", reactions: [null], can_react: true }),
        "IC_1",
      ),
    ).toBeNull();
    expect(
      parseCommentReactionsAnswer(answer({ state: "present", reactions: [] }), "IC_1"),
    ).toBeNull();
  });
});
