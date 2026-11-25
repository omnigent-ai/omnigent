import { describe, expect, it } from "vitest";
import type { Comment } from "@/hooks/useComments";
import { addressedByAgent, isAgentComment } from "./CommentsPanel";

function comment(overrides: Partial<Comment>): Comment {
  return {
    id: "c1",
    conversation_id: "f1",
    path: "plan.md",
    start_index: 10,
    end_index: 17,
    body: "Can we say Thursday?",
    status: "addressed",
    created_at: 100,
    updated_at: 100,
    created_by: "kasey@example.com",
    ...overrides,
  } as Comment;
}

describe("addressed by an agent", () => {
  it("names the agent that replied on the same text", () => {
    const person = comment({});
    const reply = comment({ id: "c2", created_by: "Product Designer (agent)", created_at: 120 });
    expect(isAgentComment(reply)).toBe(true);
    expect(addressedByAgent(person, [person, reply])).toBe("Product Designer");
    expect(addressedByAgent(reply, [person, reply])).toBeNull();
  });

  it("stays quiet without an agent reply, or while the comment is open", () => {
    const person = comment({});
    const elsewhere = comment({ id: "c2", created_by: "Lead (agent)", start_index: 0 });
    expect(addressedByAgent(person, [person, elsewhere])).toBeNull();
    expect(addressedByAgent(comment({ status: "draft" }), [person])).toBeNull();
  });
});
