import { describe, expect, it } from "vitest";
import type { TextDone, UserMessageBlock } from "./blocks";
import type { ConversationItem } from "./conversationItems";
import { itemsToBlocks } from "./itemsToBlocks";

function createdByOf(item: ConversationItem): unknown {
  return (item as Record<string, unknown>).created_by;
}

function userMessage(
  responseId: string,
  text: string,
  id: string,
  createdBy?: string,
): ConversationItem {
  return {
    id,
    response_id: responseId,
    type: "message",
    role: "user",
    status: "completed",
    content: [{ type: "input_text", text }],
    ...(createdBy !== undefined ? { created_by: createdBy } : {}),
  };
}

function assistantMessage(responseId: string, text: string, id: string): ConversationItem {
  return {
    id,
    response_id: responseId,
    type: "message",
    role: "assistant",
    status: "completed",
    model: "test-agent",
    content: [{ type: "output_text", text }],
  };
}

describe("message attribution", () => {
  it("hydrates human authors without tagging agent output", () => {
    const human = userMessage("resp_1", "Hello", "u1", "alice@example.com");
    const agent = assistantMessage("resp_1", "Hi there!", "a1");

    expect(createdByOf(human)).toBe("alice@example.com");
    expect("created_by" in agent).toBe(false);

    const blocks = itemsToBlocks([human, agent]);
    const user = blocks[0] as UserMessageBlock;
    const asst = blocks[1] as TextDone;
    expect(user.ctx.createdBy).toBe("alice@example.com");
    expect(asst.ctx.createdBy).toBeUndefined();
  });

  it("preserves distinct owner and collaborator authors", () => {
    const blocks = itemsToBlocks([
      userMessage("resp_1", "Owner message", "u_owner", "alice@example.com"),
      userMessage("resp_2", "Collaborator message", "u_collab", "bob@example.com"),
    ]);

    const userBlocks = blocks.filter((b): b is UserMessageBlock => b.type === "user_message");
    expect(userBlocks.map((b) => b.ctx.createdBy)).toEqual([
      "alice@example.com",
      "bob@example.com",
    ]);
  });

  it("leaves older user messages unattributed", () => {
    const blocks = itemsToBlocks([userMessage("resp_1", "no author", "u1")]);
    const userBlocks = blocks.filter((b): b is UserMessageBlock => b.type === "user_message");
    expect(userBlocks[0]!.ctx.createdBy).toBeUndefined();
  });
});
