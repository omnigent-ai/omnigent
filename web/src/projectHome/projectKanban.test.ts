import { describe, expect, it } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import type { StatusBucket } from "@/shell/sidebarNav";
import { filterKanbanCards, kanbanColumns, kanbanDropAction, takeArrivals } from "./projectKanban";

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

  it("reports only newly created rows as arrivals, once each", () => {
    const known = new Set(["seen"]);
    const rows = [row("seen", 990), row("new", 990), row("paged_in", 100)];
    expect(takeArrivals(rows, known, 1000)).toEqual(["new"]);
    expect(known).toEqual(new Set(["seen", "new", "paged_in"]));
    expect(takeArrivals(rows, known, 1000)).toEqual([]);
  });
});
