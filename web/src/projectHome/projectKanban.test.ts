import { describe, expect, it } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import {
  DEFAULT_AUTOMATION_PROJECT,
  SCHEDULED_TASK_LAUNCH_LABEL_KEY,
} from "@/lib/automationSessions";
import { MUTED_LABEL_KEY } from "@/lib/sessionMute";
import type { StatusBucket } from "@/shell/sidebarNav";
import {
  filterKanbanCards,
  kanbanColumns,
  kanbanDropAction,
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

  it("moves cards only between Unread and Done", () => {
    const card = (column: StatusBucket) => ({ conversation: row("a", 1), members: [], column });
    expect(kanbanDropAction(card("Unread"), "Done")).toBe("mark_read");
    expect(kanbanDropAction(card("Done"), "Unread")).toBe("mark_unread");
    expect(kanbanDropAction(card("Unread"), "Working")).toBeNull();
    expect(kanbanDropAction(card("Done"), "Draft")).toBeNull();
    expect(kanbanDropAction(card("Needs attention"), "Done")).toBeNull();
  });

  it("never drops a muted session into Unread", () => {
    const muted = { ...row("a", 1), labels: { [MUTED_LABEL_KEY]: "1" } } as Conversation;
    expect(
      kanbanDropAction({ conversation: muted, members: [muted], column: "Done" }, "Unread"),
    ).toBeNull();
    // Clearing unread output stays available.
    expect(
      kanbanDropAction({ conversation: muted, members: [muted], column: "Unread" }, "Done"),
    ).toBe("mark_read");
  });

  it("keeps cards with any session matching the search, ignoring case and padding", () => {
    const titled = (id: string, title: string) => ({ id, title }) as Conversation;
    const card = (...members: Conversation[]) => ({
      conversation: members[0],
      members,
      column: "Done" as const,
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
