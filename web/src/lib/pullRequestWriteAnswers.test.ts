import { describe, expect, it } from "vitest";

import { parseCommentAnswer, parseReplyAnswer, parseThreadAnswer } from "./pullRequestWriteAnswers";

const COMMENT = { author: "mac-octo", body: "hi", created_at: "2026-11-05T10:00:00Z", url: "u" };
const REVIEW_COMMENT = {
  id: "IC_1",
  author: "mac-octo",
  body: "hi",
  created_at: null,
  url: null,
  minimized: false,
};
const THREAD = {
  id: "PRRT_1",
  path: "src/app.js",
  side: "RIGHT",
  line: 12,
  start_line: null,
  start_side: null,
  original_line: 12,
  original_start_line: null,
  subject_type: "LINE",
  resolved: false,
  outdated: false,
  can_reply: true,
  diff_hunk: "@@ -1 +1 @@",
  comments: { data: [REVIEW_COMMENT], has_more: false, next_cursor: null },
};

describe("what a pull request write's answer must look like", () => {
  it.each([
    ["a whole comment", COMMENT, true],
    ["a comment with null author and time", { ...COMMENT, author: null, created_at: null }, true],
    ["no link", { ...COMMENT, url: null }, false],
    ["a body that isn't text", { ...COMMENT, body: 3 }, false],
    ["a time that isn't text", { ...COMMENT, created_at: 5 }, false],
    ["no time field", { author: "a", body: "b", url: "u" }, false],
    ["an author id that isn't text", { ...COMMENT, author_id: 7 }, false],
  ])("a comment answer with %s is accepted: %s", (_label, comment, expected) => {
    expect(parseCommentAnswer({ comment }) !== null).toBe(expected);
  });

  it("refuses a comment answer with no data or no comment", () => {
    expect(parseCommentAnswer(undefined)).toBeNull();
    expect(parseCommentAnswer({})).toBeNull();
  });

  it.each([
    ["a whole review comment", REVIEW_COMMENT, true],
    ["no minimized flag", { ...REVIEW_COMMENT, minimized: undefined }, true],
    ["no id", { ...REVIEW_COMMENT, id: undefined }, false],
    ["an empty id", { ...REVIEW_COMMENT, id: "" }, false],
    ["a time that isn't text", { ...REVIEW_COMMENT, created_at: 5 }, false],
    ["a minimized flag that isn't a boolean", { ...REVIEW_COMMENT, minimized: "no" }, false],
  ])("a reply answer with %s is accepted: %s", (_label, comment, expected) => {
    expect(parseReplyAnswer({ comment }) !== null).toBe(expected);
  });

  it.each([
    ["a whole thread", THREAD, true],
    ["a file thread with no lines", { ...THREAD, subject_type: "FILE", line: null }, true],
    ["a thread without optional fields", { ...THREAD, start_side: undefined }, true],
    [
      "a malformed comment inside",
      { ...THREAD, comments: { ...THREAD.comments, data: [null] } },
      false,
    ],
    [
      "a comment inside without an id",
      { ...THREAD, comments: { ...THREAD.comments, data: [{ ...REVIEW_COMMENT, id: undefined }] } },
      false,
    ],
    ["no comments", { ...THREAD, comments: { ...THREAD.comments, data: [] } }, false],
    ["a side GitHub doesn't have", { ...THREAD, side: "BOTH" }, false],
    ["a line that isn't a number", { ...THREAD, line: "12" }, false],
    ["no path", { ...THREAD, path: undefined }, false],
    ["no diff hunk", { ...THREAD, diff_hunk: undefined }, false],
    ["a resolved flag that isn't a boolean", { ...THREAD, resolved: "no" }, false],
    ["no paging fields", { ...THREAD, comments: { data: [REVIEW_COMMENT] } }, false],
    ["locally pending ids", { ...THREAD, pending_comment_ids: ["x"] }, false],
  ])("a new-conversation answer with %s is accepted: %s", (_label, thread, expected) => {
    expect(parseThreadAnswer({ thread }) !== null).toBe(expected);
  });

  it("accepts an explicit null thread, and refuses no thread field or no data", () => {
    expect(parseThreadAnswer({ thread: null })).toEqual({ thread: null });
    expect(parseThreadAnswer({})).toBeNull();
    expect(parseThreadAnswer(undefined)).toBeNull();
  });
});
