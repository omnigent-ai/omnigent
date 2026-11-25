import { describe, expect, it } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import { splitAgentRows } from "./Sidebar";

function row(id: string, family?: string): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: null,
    ...(family ? { family: { id: family } } : {}),
  } as Conversation;
}

describe("splitAgentRows", () => {
  it("keeps order within each group", () => {
    const rows = [row("s1"), row("lead"), row("s2"), row("merger")];
    const { agents, sessions } = splitAgentRows(rows, (r) => r.id === "lead" || r.id === "merger");
    expect(agents.map((r) => r.id)).toEqual(["lead", "merger"]);
    expect(sessions.map((r) => r.id)).toEqual(["s1", "s2"]);
  });

  it("keeps a session family with its head, even when a member alone looks like an agent", () => {
    const rows = [
      row("reviewer", "f1"),
      row("reviewer-sub", "f1"),
      row("s1", "f2"),
      row("s1-sub", "f2"),
    ];
    const { agents, sessions } = splitAgentRows(
      rows,
      (r) => r.id === "reviewer" || r.id === "s1-sub",
    );
    expect(agents.map((r) => r.id)).toEqual(["reviewer", "reviewer-sub"]);
    expect(sessions.map((r) => r.id)).toEqual(["s1", "s1-sub"]);
  });
});
