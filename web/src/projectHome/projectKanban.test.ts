import { describe, expect, it } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import {
  DEFAULT_AUTOMATION_PROJECT,
  SCHEDULED_TASK_LAUNCH_LABEL_KEY,
} from "@/lib/automationSessions";
import { MUTED_LABEL_KEY } from "@/lib/sessionMute";
import { statusBucket, type StatusBucket } from "@/shell/sidebarNav";
import {
  filterKanbanCards,
  kanbanColumns,
  kanbanDropAction,
  nestedAttentionLabel,
  projectBoardRows,
  takeArrivals,
} from "./projectKanban";

const family = { id: "family", representative_id: "parent", pinned: false };
const row = (id: string, created_at: number, inFamily = false) =>
  ({ id, created_at, ...(inFamily ? { family } : {}) }) as Conversation;

describe("project kanban", () => {
  it("lists one card per family in its most urgent member's column, in row order", () => {
    const rows = [
      row("done_a", 1),
      row("parent", 2, true),
      row("unread", 3),
      row("nested", 4, true),
      row("done_b", 5),
    ];
    const column: Record<string, StatusBucket> = {
      done_a: "Done",
      parent: "Done",
      unread: "Unread",
      nested: "Needs attention",
      done_b: "Done",
    };
    const columns = kanbanColumns(rows, (conversation) => column[conversation.id]);

    const ids = (bucket: StatusBucket) =>
      columns.get(bucket)!.map((card) => card.members.map((member) => member.id));
    expect([...columns.keys()]).toEqual(["Draft", "Needs attention", "Working", "Unread", "Done"]);
    expect(ids("Needs attention")).toEqual([["parent", "nested"]]);
    expect(columns.get("Needs attention")![0].conversation.id).toBe("parent");
    expect(ids("Unread")).toEqual([["unread"]]);
    expect(ids("Working")).toEqual([]);
    expect(ids("Done")).toEqual([["done_a"], ["done_b"]]);
  });

  it("drops a nested failure older than the family's latest activity", () => {
    const member = (id: string, fields: Partial<Conversation>) =>
      ({
        ...row(id, 1, true),
        labels: { "omnigent.last_task_error_at": String(fields.updated_at) },
        ...fields,
      }) as Conversation;
    const columnOf = (c: Conversation): StatusBucket =>
      c.status === "failed" || (c.pending_elicitations_count ?? 0) > 0 ? "Needs attention" : "Done";
    const cardOf = (...members: Conversation[]) =>
      [...kanbanColumns(members, columnOf).values()].flat()[0];

    const parent = member("parent", { status: "idle", updated_at: 200 });
    const staleFailure = member("failed", { status: "failed", updated_at: 100 });
    const stale = cardOf(parent, staleFailure);
    expect(stale.column).toBe("Done");
    expect(stale.attentionMembers).toEqual([]);

    const freshFailure = member("failed", { status: "failed", updated_at: 300 });
    const fresh = cardOf(parent, freshFailure);
    expect(fresh.column).toBe("Needs attention");
    expect(fresh.attentionMembers).toEqual([freshFailure]);
    expect(nestedAttentionLabel(fresh)).toBe("1 failed");

    // The worker last spoke at T100, the head at T150, then recovery failed at T200.
    const silentFailure = member("failed", {
      status: "failed",
      updated_at: 100,
      labels: { "omnigent.last_task_error_at": "200" },
    });
    const beforeFailure = member("parent", { status: "idle", updated_at: 150 });
    expect(cardOf(beforeFailure, silentFailure).attentionMembers).toEqual([silentFailure]);
    const afterFailure = member("parent", { status: "idle", updated_at: 250 });
    expect(cardOf(afterFailure, silentFailure).column).toBe("Done");

    for (const timestamp of [undefined, "", "invalid", "0", "-1", "Infinity"]) {
      const unknownFailure = member("failed", {
        status: "failed",
        updated_at: 100,
        labels: timestamp === undefined ? {} : { "omnigent.last_task_error_at": timestamp },
      });
      expect(cardOf(afterFailure, unknownFailure).attentionMembers).toEqual([unknownFailure]);
    }

    // An outstanding approval rolls up however old it is.
    const asking = member("asking", {
      status: "idle",
      pending_elicitations_count: 1,
      updated_at: 1,
    });
    const approval = cardOf(parent, asking);
    expect(approval.column).toBe("Needs attention");
    expect(nestedAttentionLabel(approval)).toBe("1 needs attention");
  });

  it("counts only fresh nested failures, ties included, and approvals on failed members", () => {
    const member = (id: string, fields: Partial<Conversation>) =>
      ({
        ...row(id, 1, true),
        labels: { "omnigent.last_task_error_at": String(fields.updated_at) },
        ...fields,
      }) as Conversation;
    const columnOf = (c: Conversation): StatusBucket =>
      c.status === "failed" || (c.pending_elicitations_count ?? 0) > 0 ? "Needs attention" : "Done";
    const cardOf = (...members: Conversation[]) =>
      [...kanbanColumns(members, columnOf).values()].flat()[0];
    const parent = member("parent", { status: "idle", updated_at: 100 });

    const older = member("older", { status: "failed", updated_at: 50 });
    const newer = member("newer", { status: "failed", updated_at: 150 });
    expect(cardOf(parent, older, newer).attentionMembers.map((m) => m.id)).toEqual(["newer"]);
    const activeSibling = member("sibling", { status: "idle", updated_at: 200 });
    const superseded = cardOf(parent, newer, activeSibling);
    expect(superseded.column).toBe("Done");
    expect(superseded.attentionMembers).toEqual([]);

    const tied = member("tied", { status: "failed", updated_at: 100 });
    expect(cardOf(parent, tied).column).toBe("Needs attention");

    const askingFailed = member("asking", {
      status: "failed",
      pending_elicitations_count: 1,
      updated_at: 1,
    });
    const asking = cardOf(parent, askingFailed);
    expect(asking.column).toBe("Needs attention");
    expect(nestedAttentionLabel(asking)).toBe("1 needs attention");
  });

  it("still rolls up an older nested turn parked on a terminal prompt, and a lone head", () => {
    const columnOf = (c: Conversation) => statusBucket(c, false, false);
    const parent = { ...row("parent", 1, true), status: "idle", updated_at: 200 } as Conversation;
    const parked = {
      ...row("parked", 2, true),
      status: "running",
      blocked_on: "permission prompt",
      updated_at: 100,
    } as Conversation;
    const [card] = kanbanColumns([parent, parked], columnOf).get("Needs attention")!;
    expect(card.attentionMembers).toEqual([parked]);
    expect(nestedAttentionLabel(card)).toBe("1 needs attention");

    const lone = { ...row("parent", 1, true), status: "failed", updated_at: 1 } as Conversation;
    const [head] = kanbanColumns([lone], columnOf).get("Needs attention")!;
    expect(head.members).toEqual([lone]);
    expect(nestedAttentionLabel(head)).toBeNull();
  });

  it("keeps a failed head in Needs attention without a nested note", () => {
    const failedHead = {
      ...row("parent", 1, true),
      status: "failed",
      updated_at: 100,
    } as Conversation;
    const newer = { ...row("nested", 2, true), status: "idle", updated_at: 200 } as Conversation;
    const columnOf = (c: Conversation): StatusBucket =>
      c.status === "failed" ? "Needs attention" : "Done";
    const [card] = kanbanColumns([failedHead, newer], columnOf).get("Needs attention")!;
    expect(card.conversation.id).toBe("parent");
    expect(nestedAttentionLabel(card)).toBeNull();
  });

  it("moves cards only between Unread and Done", () => {
    const card = (column: StatusBucket) => ({
      conversation: row("a", 1),
      members: [],
      column,
      attentionMembers: [],
    });
    expect(kanbanDropAction(card("Unread"), "Done")).toBe("mark_read");
    expect(kanbanDropAction(card("Done"), "Unread")).toBe("mark_unread");
    expect(kanbanDropAction(card("Unread"), "Working")).toBeNull();
    expect(kanbanDropAction(card("Done"), "Draft")).toBeNull();
    expect(kanbanDropAction(card("Needs attention"), "Done")).toBeNull();
  });

  it("never drops a muted session into Unread", () => {
    const muted = { ...row("a", 1), labels: { [MUTED_LABEL_KEY]: "1" } } as Conversation;
    expect(
      kanbanDropAction(
        { conversation: muted, members: [muted], column: "Done", attentionMembers: [] },
        "Unread",
      ),
    ).toBeNull();
    // Clearing unread output stays available.
    expect(
      kanbanDropAction(
        { conversation: muted, members: [muted], column: "Unread", attentionMembers: [] },
        "Done",
      ),
    ).toBe("mark_read");
  });

  it("keeps cards with any session matching the search, ignoring case and padding", () => {
    const titled = (id: string, title: string) => ({ id, title }) as Conversation;
    const card = (...members: Conversation[]) => ({
      conversation: members[0],
      members,
      column: "Done" as const,
      attentionMembers: [],
    });
    const lone = card(titled("a", "Fix login bug"));
    const withNested = card(titled("b", "Refactor"), titled("c", "Add LOGIN test"));
    const unrelated = card(titled("d", "Update docs"));
    const cards = [lone, withNested, unrelated];

    expect(filterKanbanCards(cards, "  login ")).toEqual([lone, withNested]);
    expect(filterKanbanCards(cards, "missing")).toEqual([]);
    expect(filterKanbanCards(cards, " ")).toEqual(cards);
  });

  it("adds runs shared with the viewer to the default automation board only", () => {
    const session = (id: string, fields: Partial<Conversation>) =>
      ({ id, created_at: 1, owner: "viewer", ...fields }) as Conversation;
    const own = session("own", { project_id: "automation" });
    const ownArchived = session("own_archived", { project_id: "automation", archived: true });
    const sharedRun = session("shared_run", {
      owner: "teammate",
      labels: { [SCHEDULED_TASK_LAUNCH_LABEL_KEY]: "task" },
    });
    const sharedChat = session("shared_chat", { owner: "teammate" });
    const automation = { id: "automation", name: DEFAULT_AUTOMATION_PROJECT };
    const ids = (rows: Conversation[]) => rows.map((conversation) => conversation.id);

    expect(
      ids(projectBoardRows([own, ownArchived], [own, sharedRun, sharedChat], automation, "viewer")),
    ).toEqual(["own", "shared_run"]);
    expect(
      ids(projectBoardRows([], [sharedRun], { id: "other", name: "Other project" }, "viewer")),
    ).toEqual([]);
  });

  it("reports only newly created rows as arrivals, once each", () => {
    const known = new Set(["seen"]);
    const rows = [row("seen", 990), row("new", 990), row("paged_in", 100)];
    expect(takeArrivals(rows, known, 1000)).toEqual(["new"]);
    expect(known).toEqual(new Set(["seen", "new", "paged_in"]));
    expect(takeArrivals(rows, known, 1000)).toEqual([]);
  });
});
