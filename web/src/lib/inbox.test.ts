import { describe, expect, it } from "vitest";
import type { Comment } from "@/hooks/useComments";
import type { Conversation } from "@/hooks/useConversations";
import { collectCommentInboxItems, collectInboxItems, sumPendingApprovals } from "./inbox";

function makeRow(overrides: Partial<Conversation> & { id: string }): Conversation {
  return {
    object: "conversation",
    title: null,
    created_at: 1_000,
    updated_at: 1_000,
    labels: {},
    permission_level: null,
    ...overrides,
  };
}

/** Minimal wire-shape `response.elicitation_request` event dict. */
function makeRawElicitation(
  elicitationId: string,
  params: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    type: "response.elicitation_request",
    elicitation_id: elicitationId,
    method: "elicitation/create",
    params: {
      mode: "form",
      message: `approve ${elicitationId}?`,
      requestedSchema: {},
      phase: "tool_call",
      policy_name: "ask_everything",
      content_preview: "",
      ...params,
    },
  };
}

describe("collectInboxItems", () => {
  it("parses raw event dicts into items carrying the owning session's row", () => {
    const row = makeRow({
      id: "conv_a",
      title: "Fix Stripe webhook retries",
      agent_name: "nessie",
      updated_at: 2_000,
    });
    const items = collectInboxItems([
      { row, pendingElicitations: [makeRawElicitation("elicit_1")] },
    ]);

    expect(items).toHaveLength(1);
    // The whole row rides along so the page can reuse the sidebar's
    // display-label helpers (wrapper label → "Claude Code", etc.).
    expect(items[0].row).toBe(row);
    expect(items[0].resolveSessionId).toBe("conv_a");
    // Content must survive the parse, not just the structure.
    expect(items[0].elicitation.message).toBe("approve elicit_1?");
    expect(items[0].elicitation.policyName).toBe("ask_everything");
  });

  it("routes mirrored child prompts to the child via target_session_id", () => {
    const parent = makeRow({ id: "conv_parent" });
    const items = collectInboxItems([
      {
        row: parent,
        pendingElicitations: [
          makeRawElicitation("elicit_child", { target_session_id: "conv_child" }),
        ],
      },
    ]);

    expect(items).toHaveLength(1);
    // Open-session targets the row we found the prompt under...
    expect(items[0].row.id).toBe("conv_parent");
    // ...but the verdict must POST to the session owning the Future.
    expect(items[0].resolveSessionId).toBe("conv_child");
  });

  it("dedupes a prompt mirrored into several snapshots, keeping the newest row", () => {
    const child = makeRow({ id: "conv_child", title: "child", updated_at: 3_000 });
    const parent = makeRow({ id: "conv_parent", title: "parent", updated_at: 2_000 });
    const shared = makeRawElicitation("elicit_shared", { target_session_id: "conv_child" });
    const items = collectInboxItems([
      { row: parent, pendingElicitations: [shared] },
      { row: child, pendingElicitations: [shared] },
    ]);

    expect(items).toHaveLength(1);
    expect(items[0].row.id).toBe("conv_child");
  });

  it("sorts items newest-session-first and drops malformed events", () => {
    const older = makeRow({ id: "conv_old", updated_at: 1_000 });
    const newer = makeRow({ id: "conv_new", updated_at: 5_000 });
    const items = collectInboxItems([
      {
        row: older,
        pendingElicitations: [
          makeRawElicitation("elicit_old"),
          // No elicitation_id — parseEvent rejects it.
          { type: "response.elicitation_request", params: { message: "bad" } },
        ],
      },
      { row: newer, pendingElicitations: [makeRawElicitation("elicit_new")] },
    ]);

    expect(items.map((i) => i.elicitation.elicitationId)).toEqual(["elicit_new", "elicit_old"]);
  });
});

function makeComment(overrides: Partial<Comment> & { id: string }): Comment {
  return {
    conversation_id: "conv_a",
    path: "src/main.py",
    start_index: 0,
    end_index: 10,
    body: `comment ${overrides.id}`,
    status: "draft",
    created_at: 1_000,
    updated_at: 1_000_000_000,
    anchor_content: null,
    created_by: "alice@example.com",
    ...overrides,
  };
}

describe("collectCommentInboxItems", () => {
  it("lists draft comments from other users, carrying the owning row", () => {
    const row = makeRow({ id: "conv_a", title: "Fix retries" });
    const items = collectCommentInboxItems(
      [{ row, comments: [makeComment({ id: "c1" })] }],
      new Set(),
      "bob@example.com",
    );

    expect(items).toHaveLength(1);
    // The whole row rides along (same contract as collectInboxItems)
    // so the page can reuse the sidebar's display-label helpers.
    expect(items[0].row).toBe(row);
    // Content survives the pass-through, not just the structure.
    expect(items[0].comment.body).toBe("comment c1");
  });

  it("excludes addressed comments", () => {
    // An addressed comment was resolved before the viewer got to it —
    // listing it would prompt the user to act on something finished.
    const items = collectCommentInboxItems(
      [
        {
          row: makeRow({ id: "conv_a" }),
          comments: [makeComment({ id: "c1", status: "addressed" })],
        },
      ],
      new Set(),
      "bob@example.com",
    );
    expect(items).toEqual([]);
  });

  it("excludes comments already marked seen", () => {
    const items = collectCommentInboxItems(
      [
        {
          row: makeRow({ id: "conv_a" }),
          comments: [makeComment({ id: "c_seen" }), makeComment({ id: "c_new" })],
        },
      ],
      new Set(["c_seen"]),
      "bob@example.com",
    );
    expect(items.map((i) => i.comment.id)).toEqual(["c_new"]);
  });

  it("excludes the viewer's own comments when authorship is known", () => {
    const items = collectCommentInboxItems(
      [
        {
          row: makeRow({ id: "conv_a" }),
          comments: [
            makeComment({ id: "c_mine", created_by: "alice@example.com" }),
            makeComment({ id: "c_other", created_by: "bob@example.com" }),
          ],
        },
      ],
      new Set(),
      "alice@example.com",
    );
    expect(items.map((i) => i.comment.id)).toEqual(["c_other"]);
  });

  it("keeps anonymous comments and all comments for an anonymous viewer", () => {
    // Single-user mode: created_by and the viewer id are both null —
    // a null/null match must NOT exclude everything, or the inbox is
    // permanently empty in single-user deployments.
    const anonymous = makeComment({ id: "c_anon", created_by: null });
    const fromNull = collectCommentInboxItems(
      [{ row: makeRow({ id: "conv_a" }), comments: [anonymous] }],
      new Set(),
      null,
    );
    expect(fromNull.map((i) => i.comment.id)).toEqual(["c_anon"]);

    // A known viewer still sees anonymous comments (they can't be hers).
    const fromKnown = collectCommentInboxItems(
      [{ row: makeRow({ id: "conv_a" }), comments: [anonymous] }],
      new Set(),
      "alice@example.com",
    );
    expect(fromKnown.map((i) => i.comment.id)).toEqual(["c_anon"]);
  });

  it("sorts newest first, tie-breaking same-second comments on updated_at", () => {
    const items = collectCommentInboxItems(
      [
        {
          row: makeRow({ id: "conv_a" }),
          comments: [
            makeComment({ id: "c_old", created_at: 1_000 }),
            // Same created_at second; updated_at microseconds break the tie.
            makeComment({ id: "c_tie_late", created_at: 2_000, updated_at: 2_000_000_500 }),
            makeComment({ id: "c_tie_early", created_at: 2_000, updated_at: 2_000_000_100 }),
          ],
        },
      ],
      new Set(),
      "bob@example.com",
    );
    expect(items.map((i) => i.comment.id)).toEqual(["c_tie_late", "c_tie_early", "c_old"]);
  });
});

describe("sumPendingApprovals", () => {
  it("sums counts across rows, skipping archived rows and absent counts", () => {
    const rows = [
      makeRow({ id: "a", pending_elicitations_count: 2 }),
      makeRow({ id: "b", pending_elicitations_count: 1, archived: true }),
      makeRow({ id: "c" }),
      makeRow({ id: "d", pending_elicitations_count: 3 }),
    ];
    expect(sumPendingApprovals(rows)).toBe(5);
  });
});
