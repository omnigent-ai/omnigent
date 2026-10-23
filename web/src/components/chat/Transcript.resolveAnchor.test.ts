// Find-in-chat matches name a stored item (a user message, a tool call, a reply
// chunk) plus its response id; the jump needs the bubble row that renders it.

import { describe, expect, it } from "vitest";
import type { Bubble } from "@/lib/renderItems";
import { resolveMessageAnchor } from "./Transcript";

const bubbles: Bubble[] = [
  { kind: "user", itemId: "msg_user", content: [{ type: "input_text", text: "hi" }] },
  {
    kind: "assistant",
    responseId: "resp_1",
    stableId: "resp_1",
    lifecycle: "completed",
    error: null,
    items: [
      { kind: "text", itemId: "msg_reply", text: "reply", final: true },
      {
        kind: "native_tool",
        itemId: "fc_tool",
        toolType: "shell",
        label: "ls",
        data: {},
      },
    ],
  },
];

describe("resolveMessageAnchor", () => {
  it("anchors a user message on its own bubble", () => {
    expect(resolveMessageAnchor(bubbles, "msg_user")).toBe("msg_user");
  });

  it("anchors a matched tool call or reply on the reply's bubble", () => {
    expect(resolveMessageAnchor(bubbles, "fc_tool")).toBe("resp_1");
    expect(resolveMessageAnchor(bubbles, "msg_reply")).toBe("resp_1");
  });

  it("anchors an item with no rendered row through its response id", () => {
    expect(resolveMessageAnchor(bubbles, "resp_1")).toBe("resp_1");
  });

  it("falls back to a reply unless it is the oldest loaded row with history left", () => {
    // A tool output folds into its call's row, so only its response id resolves.
    expect(resolveMessageAnchor(bubbles, "resp_1", true)).toBe("resp_1");
    const replyFirst = bubbles.slice(1);
    expect(resolveMessageAnchor(replyFirst, "resp_1", true)).toBeNull();
    expect(resolveMessageAnchor(replyFirst, "resp_1", false)).toBe("resp_1");
  });

  it("returns null for an item that is not loaded", () => {
    expect(resolveMessageAnchor(bubbles, "msg_older")).toBeNull();
  });
});
