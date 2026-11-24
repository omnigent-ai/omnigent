import { describe, expect, it } from "vitest";
import { automaticInputKind, foldBackgroundTurns } from "./backgroundTurns";
import type { Bubble, RenderItem } from "./renderItems";

const TICKET = "0123456789abcdef0123456789abcdef";

function user(itemId: string, text: string): Bubble {
  return { kind: "user", itemId, content: [{ type: "input_text", text }] };
}

function ticketUpdate(itemId: string, ticketId = TICKET): Bubble {
  return user(
    itemId,
    `Automatic ticket update for project p1. Use the orchestrator skill to handle it.\n\nTicket ${ticketId} · comment\n{"text": "hi"}`,
  );
}

function assistant(stableId: string, items: RenderItem[] = []): Bubble {
  return {
    kind: "assistant",
    responseId: stableId,
    stableId,
    lifecycle: "completed",
    error: null,
    items: [{ kind: "text", itemId: null, text: "noted", final: true }, ...items],
  };
}

function toolCall(name: string, args: Record<string, unknown>): RenderItem {
  return {
    kind: "tool",
    itemId: null,
    execution: {
      name,
      arguments: args,
      argsSummary: "",
      callId: "call",
      agentName: "agent",
      executedBy: "client",
      output: null,
    },
    output: null,
    state: "output-available",
    startedAt: null,
    duration: undefined,
  };
}

describe("automaticInputKind", () => {
  it("classifies each automatic input and leaves a person's message alone", () => {
    expect(automaticInputKind(ticketUpdate("t"))).toBe("ticket_update");
    expect(automaticInputKind(user("m", "[Message from session abc123]\nDone."))).toBe(
      "session_message",
    );
    expect(automaticInputKind(user("s", "[System: timer reminder fired]"))).toBe("system");
    expect(automaticInputKind(user("h", "What's the status?"))).toBeNull();
  });
});

describe("foldBackgroundTurns", () => {
  it("folds consecutive automatic turns between a person's turns into one row", () => {
    const bubbles = [
      user("h1", "Start the work"),
      assistant("a1"),
      ticketUpdate("t1"),
      assistant("a2"),
      user("m1", "[Message from session abc123]\nHanded off."),
      assistant("a3"),
      user("h2", "Thanks"),
      assistant("a4"),
    ];
    const rows = foldBackgroundTurns(bubbles);
    expect(rows.map((row) => row.kind)).toEqual([
      "user",
      "assistant",
      "background_updates",
      "user",
      "assistant",
    ]);
    const folded = rows[2] as Extract<Bubble, { kind: "background_updates" }>;
    expect(folded).toMatchObject({ itemId: "t1", updateCount: 2, ticketCount: 1 });
    expect(folded.bubbles).toEqual(bubbles.slice(2, 6));
  });

  it("keeps a turn that asks the person something visible", () => {
    const asks = assistant("a2", [
      toolCall("mcp__omnigent__sys_ticket_update", { labels: [{ key: "needs-human" }] }),
    ]);
    const posts = assistant("a3", [toolCall("mcp__omnigent__sys_user_inbox_post", {})]);
    const clears = assistant("a4", [toolCall("mcp__omnigent__sys_ticket_update", { labels: [] })]);
    const rows = foldBackgroundTurns([
      ticketUpdate("t1"),
      asks,
      ticketUpdate("t2"),
      posts,
      ticketUpdate("t3"),
      clears,
    ]);
    expect(rows.map((row) => row.kind)).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
      "background_updates",
    ]);
  });

  it("keeps a ticket event that marks a ticket needs-human visible", () => {
    const marked = user(
      "t2",
      `Automatic ticket update for project p1. Use the orchestrator skill to handle it.\n\nTicket ${TICKET} · updated\n{"changes": {"labels": [{"key": "needs-human"}]}}`,
    );
    const rows = foldBackgroundTurns([
      ticketUpdate("t1"),
      assistant("a1"),
      marked,
      assistant("a2"),
    ]);
    expect(rows.map((row) => row.kind)).toEqual(["background_updates", "user", "assistant"]);
  });

  it("leaves a session that never receives ticket updates unchanged", () => {
    const bubbles = [user("m1", "[Message from session abc123]\nDo the task."), assistant("a1")];
    expect(foldBackgroundTurns(bubbles)).toBe(bubbles);
  });
});
