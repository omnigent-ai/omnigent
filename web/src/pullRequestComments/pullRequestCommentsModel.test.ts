import { describe, expect, it } from "vitest";

import type { PullRequestComment } from "@/newPullRequestPage/hooks/usePullRequests";
import type { ReviewThread } from "@/lib/pullRequestReviewThreads";
import {
  type CommentsItem,
  countLabel,
  entryTime,
  filterCounts,
  filterValueTitle,
  isDraftThread,
  matchesFilters,
  matchesSearch,
  parseDiffHunk,
  shownHunkLines,
  sortItems,
  threadLineLabel,
  threadSpan,
  threadTime,
  toggleFilter,
  UNDATED,
} from "./pullRequestCommentsModel";

function thread(id: string, over: Partial<ReviewThread> = {}): ReviewThread {
  return {
    id,
    path: "src/app.ts",
    side: "RIGHT",
    line: 10,
    start_line: null,
    subject_type: "LINE",
    resolved: false,
    outdated: false,
    can_reply: true,
    diff_hunk: "",
    comments: {
      data: [
        {
          id: `${id}-1`,
          author: "ada",
          body: "First thought",
          created_at: "2026-10-01T00:00:00Z",
          url: null,
        },
      ],
      has_more: false,
      next_cursor: null,
    },
    ...over,
  };
}

function comment(author: string, body: string, created: string): PullRequestComment {
  return { author, body, created_at: created, url: null };
}

/** Threads, then conversation comments, keyed and placed in time as Activity lists them. */
function listed(
  comments: readonly PullRequestComment[],
  threads: readonly ReviewThread[],
): CommentsItem[] {
  return [
    ...threads.map((read): CommentsItem => ({
      kind: "thread",
      key: `thread:${read.id}`,
      thread: read,
      time: threadTime(read),
    })),
    ...comments.map((entry, index): CommentsItem => ({
      kind: "comment",
      key: `comment:${entry.url ?? index}`,
      comment: entry,
      time: entryTime(entry.created_at),
    })),
  ];
}

describe("entryTime and threadTime", () => {
  it("places a thread by its first published comment, and an unreadable time last", () => {
    const replied = thread("t", {
      comments: {
        data: [
          {
            id: "draft",
            author: "ada",
            body: "",
            created_at: "2026-10-09T00:00:00Z",
            url: null,
            pending: true,
          },
          { id: "c", author: "ada", body: "", created_at: "2026-10-02T00:00:00Z", url: null },
        ],
        has_more: false,
        next_cursor: null,
      },
    });
    expect(threadTime(replied)).toBe(Date.parse("2026-10-02T00:00:00Z"));
    expect(entryTime(null)).toBe(UNDATED);
    expect(entryTime("not a time")).toBe(UNDATED);
  });
});

describe("matchesFilters", () => {
  const [open, done, conversation] = listed(
    [comment("bob", "Merge requirements", "2026-10-02T00:00:00Z")],
    [thread("open"), thread("done", { resolved: true, outdated: true, path: "src/edge.rs" })],
  );

  it("narrows by author, file and state, any value within a field and every field", () => {
    const passing = (filters: Parameters<typeof matchesFilters>[1]) =>
      [open!, done!, conversation!]
        .filter((item) => matchesFilters(item, filters))
        .map((item) => item.key);
    expect(passing([{ field: "state", values: ["outdated"] }])).toEqual(["thread:done"]);
    // A conversation comment is current, and has no file.
    expect(passing([{ field: "state", values: ["current"] }])).toEqual([
      "thread:open",
      "comment:0",
    ]);
    expect(passing([{ field: "file", values: ["src/edge.rs", "src/app.ts"] }])).toEqual([
      "thread:open",
      "thread:done",
    ]);
    expect(
      passing([
        { field: "author", values: ["ada"] },
        { field: "file", values: ["src/edge.rs"] },
      ]),
    ).toEqual(["thread:done"]);
    expect(passing([{ field: "author", values: [] }])).toHaveLength(3);
  });
});

describe("search", () => {
  it("reads a comment's author and body, and a thread's file", () => {
    const [edge, conversation] = listed(
      [comment("bob", "Merge requirements", "2026-10-02T00:00:00Z")],
      [thread("done", { path: "src/edge.rs" })],
    );
    expect(matchesSearch(conversation!, "bob merge")).toBe(true);
    expect(matchesSearch(edge!, "edge.rs")).toBe(true);
    expect(matchesSearch(conversation!, "edge.rs")).toBe(false);
  });

  it("needs every word, and leaves hidden comments' text out", () => {
    const [item] = listed(
      [],
      [
        thread("t", {
          comments: {
            data: [
              {
                id: "c",
                author: "ada",
                body: "secret plan",
                created_at: null,
                url: null,
                minimized: true,
              },
            ],
            has_more: false,
            next_cursor: null,
          },
        }),
      ],
    );
    expect(matchesSearch(item!, "ada app.ts")).toBe(true);
    expect(matchesSearch(item!, "secret")).toBe(false);
    expect(matchesSearch(item!, "  ")).toBe(true);
  });
});

describe("filters", () => {
  it("toggles values within a field and drops the field once empty", () => {
    let filters = toggleFilter([], "author", "ada");
    filters = toggleFilter(filters, "author", "bob");
    expect(filters).toEqual([{ field: "author", values: ["ada", "bob"] }]);
    filters = toggleFilter(toggleFilter(filters, "author", "ada"), "author", "bob");
    expect(filters).toEqual([]);
  });

  it("counts values per field and titles them", () => {
    const counts = filterCounts(
      listed(
        [comment("bob", "x", "2026-10-02T00:00:00Z")],
        [thread("t"), thread("old", { outdated: true })],
      ),
    );
    expect([...counts.author]).toEqual([
      ["ada", 2],
      ["bob", 1],
    ]);
    expect([...counts.state]).toEqual([
      ["current", 2],
      ["outdated", 1],
    ]);
    expect(filterValueTitle("file", "src/deep/app.ts")).toBe("app.ts");
    expect(filterValueTitle("state", "outdated")).toBe("Outdated");
  });
});

describe("sortItems", () => {
  it("reverses for newest first and orders by file, then line, for By File", () => {
    const items = listed(
      [comment("bob", "x", "2026-10-05T00:00:00Z")],
      [
        thread("b", { path: "b.ts", line: 3 }),
        thread("a2", { path: "a.ts", line: 9 }),
        thread("a1", { path: "a.ts", line: 2 }),
      ],
    );
    expect(sortItems(items, "newest")[0]!.kind).toBe("comment");
    expect(
      sortItems(items, "file").map((item) =>
        item.key.split(":")[0] === "comment" ? "comment" : item.key,
      ),
    ).toEqual(["comment", "thread:a1", "thread:a2", "thread:b"]);
  });

  const at = (day: number) => `2026-10-0${day}T00:00:00Z`;
  const reviewComment = (id: string, created: string | null, pending = false) => ({
    id,
    author: "ada",
    body: id,
    created_at: created,
    url: null,
    ...(pending ? { pending: true, pending_review_id: "review-1" } : {}),
  });
  const withComments = (id: string, comments: ReturnType<typeof reviewComment>[]) =>
    thread(id, { comments: { data: comments, has_more: false, next_cursor: null } });
  const keys = (items: readonly { key: string }[]) => items.map((item) => item.key);

  it("puts drafts first in Newest First and last in Oldest First, undated after dated", () => {
    const items = listed(
      [
        {
          ...comment("bob", "undated", ""),
          created_at: null,
          url: "https://example.com/c/undated",
        },
        { ...comment("bob", "late", at(4)), url: "https://example.com/c/late" },
      ],
      [
        withComments("draft", [reviewComment("d1", at(9), true)]),
        withComments("early", [reviewComment("e1", at(1))]),
        // A published thread with a pending reply is placed by its published comment.
        withComments("replied", [reviewComment("r1", at(2)), reviewComment("r2", at(8), true)]),
        // No readable time and published: unknown, never mistaken for a draft.
        withComments("undated-thread", [reviewComment("u1", null)]),
      ],
    );
    expect(keys(sortItems(items, "newest"))).toEqual([
      "thread:draft",
      "comment:https://example.com/c/late",
      "thread:replied",
      "thread:early",
      "thread:undated-thread",
      "comment:https://example.com/c/undated",
    ]);
    expect(keys(sortItems(items, "oldest"))).toEqual([
      "thread:early",
      "thread:replied",
      "comment:https://example.com/c/late",
      "thread:undated-thread",
      "comment:https://example.com/c/undated",
      "thread:draft",
    ]);
  });

  it("keeps a thread's replies, pending ones too, in reading order whichever way it sorts", () => {
    const replies = [
      reviewComment("first", at(1)),
      reviewComment("second", at(3)),
      reviewComment("draft-reply", at(5), true),
    ];
    const items = listed([comment("bob", "x", at(6))], [withComments("t", replies)]);
    for (const sort of ["newest", "oldest", "file"] as const) {
      const sorted = sortItems(items, sort).find((item) => item.kind === "thread");
      expect(
        sorted?.kind === "thread" && sorted.thread.comments.data.map((reply) => reply.id),
      ).toEqual(["first", "second", "draft-reply"]);
    }
  });

  it("keeps the loaded order for equal times and among drafts, in both directions", () => {
    const items = listed(
      [],
      [
        withComments("same-a", [reviewComment("a", at(2))]),
        withComments("same-b", [reviewComment("b", at(2))]),
        withComments("draft-a", [reviewComment("da", at(7), true)]),
        withComments("draft-b", [reviewComment("db", at(3), true)]),
      ],
    );
    // Drafts aren't ordered by time among themselves: they keep the order they were read in.
    expect(keys(sortItems(items, "newest"))).toEqual([
      "thread:draft-a",
      "thread:draft-b",
      "thread:same-a",
      "thread:same-b",
    ]);
    expect(keys(sortItems(items, "oldest")).slice(0, 2)).toEqual([
      "thread:same-a",
      "thread:same-b",
    ]);
  });

  it("sorts drafts by file and line like any thread under By File", () => {
    const items = listed(
      [],
      [
        withComments("draft", [reviewComment("d", at(9), true)]),
        { ...withComments("later-file", [reviewComment("z", at(1))]), path: "z.ts" },
      ],
    );
    expect(keys(sortItems(items, "file"))).toEqual(["thread:draft", "thread:later-file"]);
  });
});

describe("isDraftThread", () => {
  it("is a thread whose every comment is pending, never an empty one or a comment", () => {
    const [draft, mixed, empty, conversation] = listed(
      [comment("bob", "x", "2026-10-09T00:00:00Z")],
      [
        thread("draft", {
          comments: {
            data: [
              { id: "d", author: "ada", body: "", created_at: null, url: null, pending: true },
            ],
            has_more: false,
            next_cursor: null,
          },
        }),
        thread("mixed", {
          comments: {
            data: [
              { id: "m1", author: "ada", body: "", created_at: "2026-10-01T00:00:00Z", url: null },
              { id: "m2", author: "ada", body: "", created_at: null, url: null, pending: true },
            ],
            has_more: false,
            next_cursor: null,
          },
        }),
        thread("empty", { comments: { data: [], has_more: false, next_cursor: null } }),
      ],
    );
    const byKey = new Map([draft, mixed, empty, conversation].map((item) => [item!.key, item!]));
    expect(isDraftThread(byKey.get("thread:draft")!)).toBe(true);
    expect(isDraftThread(byKey.get("thread:mixed")!)).toBe(false);
    expect(isDraftThread(byKey.get("thread:empty")!)).toBe(false);
    expect(isDraftThread([...byKey.values()].find((item) => item.kind === "comment")!)).toBe(false);
  });
});

describe("threadLineLabel and threadSpan", () => {
  it("names current lines, original lines for outdated threads, and files", () => {
    expect(threadLineLabel(thread("t"))).toBe("Line 10");
    expect(threadLineLabel(thread("t", { start_line: 4 }))).toBe("Lines 4–10");
    expect(threadLineLabel(thread("t", { line: null, original_line: 273 }))).toBe("Line 273");
    expect(threadLineLabel(thread("t", { line: null, original_line: null }))).toBe("Outdated");
    expect(threadLineLabel(thread("t", { subject_type: "FILE" }))).toBe("File");
    expect(threadSpan(thread("t", { start_line: 4 }))).toBe(7);
    expect(threadSpan(thread("t", { line: null, original_line: 9, original_start_line: 8 }))).toBe(
      2,
    );
    expect(threadSpan(thread("t"))).toBe(1);
  });
});

describe("parseDiffHunk", () => {
  it("numbers old and new lines from the header and skips no-newline markers", () => {
    const lines = parseDiffHunk(
      "@@ -5,2 +7,3 @@\n context\n-gone\n+added\n+more\n\\ No newline at end of file",
    );
    expect(lines).toEqual([
      { kind: "context", text: "context", oldLine: 5, newLine: 7 },
      { kind: "deletion", text: "gone", oldLine: 6 },
      { kind: "addition", text: "added", newLine: 8 },
      { kind: "addition", text: "more", newLine: 9 },
    ]);
  });

  it("shows the span and three lines above it, at least four", () => {
    expect(shownHunkLines(273, 1)).toBe(4);
    expect(shownHunkLines(273, 6)).toBe(9);
    expect(shownHunkLines(2, 1)).toBe(2);
  });
});

describe("countLabel", () => {
  it("marks a lower bound", () => {
    expect(countLabel(11, true)).toBe("11+");
    expect(countLabel(0, false)).toBe("0");
  });
});
