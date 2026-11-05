import { afterEach, expect, it } from "vitest";
import { formatLocalReviewComments } from "./localReviewComments";
import type { Comment } from "@/hooks/useComments";
import {
  clearSessionDrafts,
  getSessionDraft,
  hasSessionDraft,
  setSessionDraft,
} from "./sessionDrafts";
import {
  requestLocalReviewContext,
  updateLocalReviewContext,
  removeLocalReviewContext,
} from "./localReviewContext";

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
  anchor_metadata: {
    pr_url: "https://github.com/acme/app/pull/1",
    side: "LEFT",
    line: 9,
    start_line: 7,
    head_sha: "head",
    base_sha: "base",
  },
};

afterEach(clearSessionDrafts);

it("includes comment references and historical coordinates for native agents", () => {
  const message = formatLocalReviewComments([
    comment,
    {
      ...comment,
      id: "comment-2",
      anchor_metadata: { ...comment.anchor_metadata!, side: "RIGHT", line: 10, start_line: null },
    },
  ]);
  expect(message).toContain("[Comment comment-1] old.py (old lines 7–9)");
  expect(message).toContain("[Comment comment-2] old.py (new lines 10)");
  expect(message).toContain("Base: base; head: head");
  expect(message).toContain("Keep this check");
  expect(message).toContain("PR: https://github.com/acme/app/pull/1");
});

it("preserves attached context through draft writes, updates edits and removes addressed or deleted comments", () => {
  requestLocalReviewContext(comment);
  requestLocalReviewContext(comment);
  expect(hasSessionDraft("session")).toBe(true);
  setSessionDraft("session", { text: "Please check", files: [] });
  expect(getSessionDraft("session")?.reviewComments).toEqual([comment]);
  updateLocalReviewContext({ ...comment, body: "Updated check" });
  expect(getSessionDraft("session")?.reviewComments?.[0].body).toBe("Updated check");
  updateLocalReviewContext({ ...comment, status: "addressed" });
  expect(getSessionDraft("session")?.reviewComments).toEqual([]);
  updateLocalReviewContext(comment);
  expect(getSessionDraft("session")?.reviewComments).toEqual([]);
  requestLocalReviewContext(comment);
  removeLocalReviewContext("another-session", comment.id);
  expect(getSessionDraft("session")?.reviewComments).toEqual([comment]);
  removeLocalReviewContext("session", comment.id);
  expect(getSessionDraft("session")?.text).toBe("Please check");
  expect(getSessionDraft("session")?.reviewComments).toEqual([]);
});

it.each([
  ["path", { path: "different.py" }],
  ["PR", { anchor_metadata: { ...comment.anchor_metadata!, pr_url: "https://example.test/2" } }],
  ["side", { anchor_metadata: { ...comment.anchor_metadata!, side: "RIGHT" } }],
  ["line", { anchor_metadata: { ...comment.anchor_metadata!, line: 10 } }],
  ["range", { anchor_metadata: { ...comment.anchor_metadata!, start_line: 8 } }],
  ["head", { anchor_metadata: { ...comment.anchor_metadata!, head_sha: "different" } }],
  ["base", { anchor_metadata: { ...comment.anchor_metadata!, base_sha: "different" } }],
] satisfies [string, Partial<Comment>][])(
  "rejects edits that change the attached %s coordinate",
  (_coordinate, changes) => {
    requestLocalReviewContext(comment);
    for (const status of ["draft", "addressed"] as const) {
      expect(() => updateLocalReviewContext({ ...comment, ...changes, status })).toThrow(
        "Your attached comment is unchanged",
      );
      expect(getSessionDraft("session")?.reviewComments).toEqual([comment]);
    }
  },
);
