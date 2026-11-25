import { describe, expect, it } from "vitest";
import type { Comment } from "@/hooks/useComments";
import type { Conversation } from "@/hooks/useConversations";
import {
  collectCommentInboxItems,
  collectInboxItems,
  collectUnreadInboxItems,
  groupInboxEntries,
  groupInboxEntriesByApproval,
  inboxTimeGroup,
  isTicketApprovalRequest,
  sumPendingApprovals,
} from "./inbox";

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

  it("excludes unauthored comments — nothing to show as someone else's", () => {
    // A null author means single-user mode or a legacy pre-attribution
    // comment. The inbox only surfaces comments an identifiable *other*
    // person left, so an unauthored comment never qualifies — regardless
    // of whether the viewer is known. This is what keeps a private /
    // single-user session's inbox empty instead of echoing your own
    // comments back at you.
    const anonymous = makeComment({ id: "c_anon", created_by: null });
    const fromNull = collectCommentInboxItems(
      [{ row: makeRow({ id: "conv_a" }), comments: [anonymous] }],
      new Set(),
      null,
    );
    expect(fromNull).toEqual([]);

    const fromKnown = collectCommentInboxItems(
      [{ row: makeRow({ id: "conv_a" }), comments: [anonymous] }],
      new Set(),
      "alice@example.com",
    );
    expect(fromKnown).toEqual([]);
  });

  it("shows another user's comment on a shared session, hides your own", () => {
    // A comment can only carry another user's `created_by` if they had
    // access — so an other-authored comment implies the session is shared.
    // Your own comment on the same shared session stays out of the inbox.
    const items = collectCommentInboxItems(
      [
        {
          row: makeRow({ id: "conv_shared" }),
          comments: [
            makeComment({ id: "c_mine", created_by: "alice@example.com" }),
            makeComment({ id: "c_theirs", created_by: "bob@example.com" }),
          ],
        },
      ],
      new Set(),
      "alice@example.com",
    );
    expect(items.map((i) => i.comment.id)).toEqual(["c_theirs"]);
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

describe("collectUnreadInboxItems", () => {
  // Stand-in for the sidebar dot's predicate: unseen when finished and
  // newer than a fixed baseline.
  const unseenAfter =
    (baseline: number) => (_id: string, updatedAt: number, status: string | undefined) =>
      status !== "running" && status !== undefined && updatedAt > baseline;

  it("lists unseen sessions newest first, tagging failed turns as errors", () => {
    const rows = [
      makeRow({ id: "old", status: "idle", updated_at: 2_000 }),
      makeRow({ id: "new", status: "failed", updated_at: 3_000 }),
      makeRow({ id: "seen", status: "idle", updated_at: 500 }),
    ];
    const items = collectUnreadInboxItems(rows, unseenAfter(1_000));
    expect(items.map((i) => [i.row.id, i.kind])).toEqual([
      ["new", "error"],
      ["old", "done"],
    ]);
  });

  it("lists only sessions whose attention is unseen, unless marked unread", () => {
    // Automatic turns move updated_at without attention_at; they stay out.
    const rows = [
      makeRow({ id: "asked", status: "idle", updated_at: 3_000, attention_at: 2_500 }),
      makeRow({ id: "automatic", status: "idle", updated_at: 3_000, attention_at: 900 }),
      makeRow({ id: "never", status: "idle", updated_at: 3_000, attention_at: null }),
      makeRow({ id: "flagged", status: "idle", updated_at: 3_000, attention_at: null }),
      makeRow({ id: "unstamped", status: "idle", updated_at: 3_000, attention_at: 0 }),
      makeRow({ id: "client-built", status: "idle", updated_at: 3_000 }),
    ];
    const marked = (id: string) => id === "flagged";
    const items = collectUnreadInboxItems(rows, unseenAfter(1_000), marked, true);
    expect(items.map((i) => i.row.id).sort()).toEqual(["asked", "flagged"]);
    // A server without the capability lists every unseen session.
    expect(collectUnreadInboxItems(rows, unseenAfter(1_000), marked, false)).toHaveLength(6);
  });

  it("lists another person's finished session only for its owner", () => {
    // A project member's session reaches the viewer at manage/edit level, not as owner.
    const rows = [
      makeRow({
        id: "mine",
        status: "idle",
        updated_at: 3_000,
        attention_at: 2_500,
        permission_level: 4,
      }),
      makeRow({
        id: "member",
        status: "idle",
        updated_at: 3_000,
        attention_at: 2_500,
        permission_level: 2,
      }),
      makeRow({
        id: "managed",
        status: "idle",
        updated_at: 3_000,
        attention_at: 2_500,
        permission_level: 3,
      }),
    ];
    const marked = (id: string) => id === "managed";
    const items = collectUnreadInboxItems(rows, unseenAfter(1_000), marked, true);
    expect(items.map((i) => i.row.id).sort()).toEqual(["managed", "mine"]);
  });

  it("skips archived rows and rows already listed as approvals", () => {
    // A session with a pending prompt shows as its approval card; listing it
    // again as unread would put one session in the inbox twice.
    const rows = [
      makeRow({ id: "archived", status: "idle", updated_at: 2_000, archived: true }),
      makeRow({ id: "awaiting", status: "idle", updated_at: 2_000, pending_elicitations_count: 1 }),
      makeRow({ id: "unread", status: "idle", updated_at: 2_000 }),
    ];
    const items = collectUnreadInboxItems(rows, unseenAfter(1_000));
    expect(items.map((i) => i.row.id)).toEqual(["unread"]);
  });

  it("defers to the predicate, so running sessions stay out", () => {
    const rows = [makeRow({ id: "running", status: "running", updated_at: 2_000 })];
    expect(collectUnreadInboxItems(rows, unseenAfter(1_000))).toEqual([]);
  });
});

describe("inboxTimeGroup", () => {
  // Wednesday 2026-10-07 15:00 local time.
  const now = new Date(2026, 9, 7, 15, 0, 0);
  const at = (date: Date) => date.getTime() / 1000;

  it("buckets by recency, using local calendar days", () => {
    expect(inboxTimeGroup(at(new Date(2026, 9, 7, 14, 57)), now)).toBe("Just now");
    expect(inboxTimeGroup(at(new Date(2026, 9, 7, 0, 5)), now)).toBe("Earlier today");
    // Late last night is "Yesterday" even though it's under 24 hours ago.
    expect(inboxTimeGroup(at(new Date(2026, 9, 6, 23, 50)), now)).toBe("Yesterday");
    expect(inboxTimeGroup(at(new Date(2026, 9, 1, 9, 0)), now)).toBe("This week");
    expect(inboxTimeGroup(at(new Date(2026, 8, 30, 9, 0)), now)).toBe("Older");
  });

  it("treats a timestamp slightly in the future (clock skew) as just now", () => {
    expect(inboxTimeGroup(at(new Date(2026, 9, 7, 15, 1)), now)).toBe("Just now");
  });
});

describe("groupInboxEntries", () => {
  it("sorts newest first and groups consecutive entries by recency", () => {
    const now = new Date(2026, 9, 7, 15, 0, 0);
    const minutesAgo = (m: number) => now.getTime() / 1000 - m * 60;
    const groups = groupInboxEntries(
      [
        { id: "yesterday", at: minutesAgo(16 * 60) },
        { id: "fresh", at: minutesAgo(1) },
        { id: "morning", at: minutesAgo(5 * 60) },
        { id: "noon", at: minutesAgo(3 * 60) },
      ],
      now,
    );
    expect(groups.map((g) => [g.label, g.entries.map((e) => e.id)])).toEqual([
      ["Just now", ["fresh"]],
      ["Earlier today", ["noon", "morning"]],
      ["Yesterday", ["yesterday"]],
    ]);
  });
});

describe("groupInboxEntriesByApproval", () => {
  it("lists questions for the viewer apart from sign-offs, both first", () => {
    const now = new Date("2026-01-10T12:00:00Z");
    const at = now.getTime() / 1000;
    const entries = [
      { id: "question", at: at - 10, ask_kind: "question" },
      { id: "sign-off", at: at - 5, ask_kind: "sign-off" },
      { id: "reply", at: at - 1 },
    ];
    const groups = groupInboxEntriesByApproval(
      entries,
      (entry) => entry.ask_kind !== undefined,
      now,
      (entry) => entry.ask_kind === "question",
    );
    expect(groups.map((group) => [group.label, group.entries.map((entry) => entry.id)])).toEqual([
      ["Questions for you", ["question"]],
      ["Needs your approval", ["sign-off"]],
      ["Just now", ["reply"]],
    ]);
  });

  it("puts what needs approval first, newest first, and keeps the rest in time groups", () => {
    const now = new Date("2026-01-10T12:00:00Z");
    const at = (hoursAgo: number) => now.getTime() / 1000 - hoursAgo * 3600;
    const entries = [
      { id: "fyi-new", at: at(0), approval: false },
      { id: "ask-old", at: at(72), approval: true },
      { id: "ask-new", at: at(1), approval: true },
      { id: "fyi-old", at: at(30), approval: false },
    ];
    const groups = groupInboxEntriesByApproval(entries, (entry) => entry.approval, now);
    expect(groups.map((group) => [group.label, group.entries.map((entry) => entry.id)])).toEqual([
      ["Needs your approval", ["ask-new", "ask-old"]],
      ["Just now", ["fyi-new"]],
      ["Yesterday", ["fyi-old"]],
    ]);
    expect(groupInboxEntriesByApproval(entries.slice(0, 1), () => false, now)[0].approval).toBe(
      false,
    );
  });

  it("counts only open asks, not a cc or an unknown reason, as needing approval", () => {
    expect(
      ["owner", "product-ok", "risk-ok", "cc", "mention"].map((reason) =>
        isTicketApprovalRequest({ reason }),
      ),
    ).toEqual([true, true, true, false, false]);
  });
});
