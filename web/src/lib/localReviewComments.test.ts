import { expect, it } from "vitest";
import { formatLocalReviewComments } from "./localReviewComments";
import type { Comment } from "@/hooks/useComments";

it("includes comment references and historical coordinates for native agents", () => {
  const comment: Comment = {
    id: "comment-1",
    conversation_id: "session",
    path: "old.py",
    start_index: 0,
    end_index: 0,
    body: "Keep this check",
    status: "draft",
    created_at: 0,
    updated_at: 0,
    created_by: null,
    anchor_content: null,
    review_anchor: {
      pr_url: "https://github.com/acme/app/pull/1",
      side: "LEFT",
      line: 9,
      start_line: 7,
      head_sha: "head",
      base_sha: "base",
    },
  };
  const message = formatLocalReviewComments([
    comment,
    {
      ...comment,
      id: "comment-2",
      review_anchor: { ...comment.review_anchor!, side: "RIGHT", line: 10, start_line: null },
    },
  ]);
  expect(message).toContain("[Comment comment-1] old.py (old lines 7–9)");
  expect(message).toContain("[Comment comment-2] old.py (new lines 10)");
  expect(message).toContain("Base: base; head: head");
  expect(message).toContain("Keep this check");
  expect(message).toContain("PR: https://github.com/acme/app/pull/1");
});
