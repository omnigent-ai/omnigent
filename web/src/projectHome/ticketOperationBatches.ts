import type { Bubble, RenderItem } from "@/lib/renderItems";

export interface TicketOperationBatch {
  bubbleId: string;
  ticketIds: string[];
  kind: "created" | "updated" | "mixed";
}

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function ticketResult(output: string): Record<string, unknown> | null {
  try {
    const result: unknown = JSON.parse(output);
    if (!record(result) || result.error || result.isError) return null;
    if (
      typeof result.id === "string" &&
      typeof result.project_id === "string" &&
      typeof result.title === "string" &&
      typeof (result.stage_id ?? result.status) === "string"
    )
      return result;
    return null;
  } catch {
    return null;
  }
}

function ticketOperation(item: RenderItem, projectId: string) {
  if (item.kind !== "tool" || item.state !== "output-available" || item.output === null)
    return null;
  const name = item.execution.name.split("__").at(-1);
  if (name !== "sys_ticket_create" && name !== "sys_ticket_update" && name !== "sys_ticket_comment")
    return null;
  const result = ticketResult(item.output);
  if (!result || result.project_id !== projectId) return null;
  return { id: result.id as string, created: name === "sys_ticket_create" };
}

/** One card per response, even when its work and answer are separate bubbles. */
export function ticketOperationBatches(
  bubbles: Bubble[],
  projectId: string,
): TicketOperationBatch[] {
  const turns = new Map<string, { bubbleId: string; operations: Map<string, boolean> }>();
  for (const bubble of bubbles) {
    if (bubble.kind !== "assistant") continue;
    const key = bubble.responseId || bubble.stableId;
    const turn = turns.get(key) ?? {
      bubbleId: bubble.stableId,
      operations: new Map<string, boolean>(),
    };
    turn.bubbleId = bubble.stableId;
    for (const item of bubble.items) {
      const operation = ticketOperation(item, projectId);
      if (operation)
        turn.operations.set(
          operation.id,
          operation.created || turn.operations.get(operation.id) === true,
        );
    }
    turns.set(key, turn);
  }
  return [...turns.values()]
    .filter((turn) => turn.operations.size > 0)
    .map((turn) => ({
      bubbleId: turn.bubbleId,
      ticketIds: [...turn.operations.keys()],
      kind: [...turn.operations.values()].every(Boolean)
        ? "created"
        : [...turn.operations.values()].some(Boolean)
          ? "mixed"
          : "updated",
    }));
}
