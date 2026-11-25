import { describe, expect, it } from "vitest";

import { restoredThreadsOf, savedThreadsOf } from "@/lib/pullRequestDetailsCache";
import type { ReviewThread, ReviewThreadsPage } from "@/lib/pullRequestReviewThreads";
import {
  type CachedThreads,
  historyOf,
  reconcileThreads,
  withChangedComment,
  withMoreComments,
} from "@/lib/reviewThreadsHistory";

const thread = (id: string, comments: string[], more?: string): ReviewThread =>
  ({
    id,
    path: "src/app.ts",
    line: 1,
    is_resolved: false,
    comments: {
      object: "list",
      data: comments.map((commentId) => ({ id: commentId, body: commentId, author: "carol" })),
      has_more: !!more,
      next_cursor: more ?? null,
    },
  }) as unknown as ReviewThread;

const page = (threads: ReviewThread[]): ReviewThreadsPage & { read_as: string } =>
  ({
    object: "list",
    data: threads,
    has_more: false,
    next_cursor: null,
    head_sha: "b".repeat(40),
    base_sha: "a".repeat(40),
    read_as: "alice",
  }) as unknown as ReviewThreadsPage & { read_as: string };

const replies = (ids: string[]) =>
  ({
    object: "list",
    data: ids.map((id) => ({ id, body: id, author: "carol" })),
    has_more: false,
    next_cursor: null,
  }) as never;

/** A page with two threads listed first, the second with its replies paged. */
function pagedPage(): CachedThreads {
  const first = reconcileThreads(
    page([thread("t1", ["c1"]), thread("t2", ["c2"], "r2")]),
    undefined,
    100,
  );
  return withMoreComments(first, { id: "t2" }, replies(["c3"]), 200);
}

describe("historyOf", () => {
  it("names the threads listed first and those whose replies were paged", () => {
    expect(historyOf(pagedPage())).toEqual({
      first_page_ids: ["t1", "t2"],
      paged_comment_ids: ["t2"],
    });
  });

  it("names no thread the page no longer shows", () => {
    const deleted = withChangedComment(pagedPage(), "t2", "c2", () => null);
    const gone = withChangedComment(deleted, "t2", "c3", () => null) as CachedThreads;
    expect(gone.data.map((value) => value.id)).toEqual(["t1"]);
    expect(historyOf(gone)).toEqual({ first_page_ids: ["t1"] });
  });
});

describe("kept conversations keep their history", () => {
  it("restores what the next first page merges by", () => {
    const saved = savedThreadsOf(pagedPage())!;
    const restored = restoredThreadsOf(JSON.parse(JSON.stringify(saved)));
    expect(restored.first_page_ids).toEqual(["t1", "t2"]);
    expect(restored.paged_comment_ids).toEqual(["t2"]);
    // So a later first page keeps the paged reply and drops what it no longer lists.
    const next = reconcileThreads(page([thread("t2", ["c2"], "r2")]), restored, 300);
    expect(
      next.data.map((value) => [value.id, value.comments.data.map((item) => item.id)]),
    ).toEqual([["t2", ["c2", "c3"]]]);
  });

  it("keeps no history for a thread the page doesn't show", () => {
    const shown = { ...pagedPage(), paged_comment_ids: ["t2", "t9"], first_page_ids: ["t9", "t1"] };
    const saved = savedThreadsOf(shown);
    expect(saved?.kind === "threads" && saved.page).toMatchObject({
      first_page_ids: ["t1"],
      paged_comment_ids: ["t2"],
    });
  });
});
