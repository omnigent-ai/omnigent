import { describe, expect, it } from "vitest";
import type { Bubble, RenderItem } from "@/lib/renderItems";
import { ticketOperationBatches } from "./ticketOperationBatches";

function operation(
  id: string,
  name = "sys_ticket_create",
  overrides: Partial<Extract<RenderItem, { kind: "tool" }>> = {},
): RenderItem {
  const output = JSON.stringify({ id, project_id: "project", title: "Ticket", stage_id: "open" });
  return {
    kind: "tool",
    itemId: `item-${id}`,
    state: "output-available",
    output,
    startedAt: null,
    duration: undefined,
    execution: {
      name,
      arguments: {},
      argsSummary: "",
      callId: `call-${id}`,
      agentName: "lead",
      executedBy: "server",
      output,
    },
    ...overrides,
  };
}

function response(stableId: string, items: RenderItem[], responseId = "turn"): Bubble {
  return { kind: "assistant", stableId, responseId, lifecycle: "completed", error: null, items };
}

describe("ticket operation batches", () => {
  it("deduplicates updates and attaches one batch to the last block of the same response", () => {
    const bubbles = [
      response("work", [operation("one"), operation("two", "sys_ticket_update")]),
      response("answer", [
        operation("one", "sys_ticket_comment"),
        { kind: "text", itemId: "answer", text: "Done", final: true },
      ]),
      response("later", [operation("three", "mcp__omnigent__sys_ticket_create")], "next-turn"),
    ];
    expect(ticketOperationBatches(bubbles, "project")).toEqual([
      { bubbleId: "answer", ticketIds: ["one", "two"], kind: "mixed" },
      { bubbleId: "later", ticketIds: ["three"], kind: "created" },
    ]);
  });

  it.each([
    operation("one", "sys_ticket_get"),
    operation("one", "sys_ticket_create", { state: "input-available" }),
    operation("one", "sys_ticket_create", { state: "output-error" }),
    operation("one", "sys_ticket_create", { output: null }),
    operation("one", "sys_ticket_create", { output: "Created three tickets" }),
    operation("one", "sys_ticket_create", {
      output: JSON.stringify({ error: "Permission denied" }),
    }),
    operation("one", "sys_ticket_create", {
      output: JSON.stringify({
        id: "one",
        project_id: "elsewhere",
        title: "Other project",
        stage_id: "open",
      }),
    }),
    { kind: "text", itemId: "claim", text: "I created 12 tickets.", final: true } as RenderItem,
  ])("does not invent a successful ticket operation from %j", (item) => {
    expect(ticketOperationBatches([response("answer", [item])], "project")).toEqual([]);
  });

  it("keeps the complete distinct ID selection for a large batch", () => {
    const items = Array.from({ length: 40 }, (_, index) => operation(`ticket-${index}`));
    expect(
      ticketOperationBatches([response("answer", items)], "project")[0].ticketIds,
    ).toHaveLength(40);
  });
});
