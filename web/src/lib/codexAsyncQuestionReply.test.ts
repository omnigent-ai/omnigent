import { describe, expect, it } from "vitest";

import { codexAsyncQuestionReplyText } from "./codexAsyncQuestionReply";

const item = (index: number) => JSON.stringify(["request_user_input_async", "call_abc", index]);
const reply = (entries: unknown) =>
  `<send_user_message_question_reply>\n${JSON.stringify(entries)}\n</send_user_message_question_reply>`;

describe("codexAsyncQuestionReplyText", () => {
  it("shows only the answers of a reply", () => {
    const text = reply([
      { answer: "Blue", question: "Color?", questionItemId: item(0) },
      { answer: "Large", question: "Size?", questionItemId: item(1) },
    ]);
    expect(codexAsyncQuestionReplyText(text)).toBe("Blue\nLarge");
  });

  it.each([
    ["ordinary text", "Use Blue"],
    [
      "a reply quoted inside other text",
      `Explain this: ${reply([{ answer: "Blue", questionItemId: item(0) }])}`,
    ],
    ["malformed JSON", "<send_user_message_question_reply>\n[{</send_user_message_question_reply>"],
    [
      "an entry for another tool",
      reply([{ answer: "Blue", questionItemId: '["other","call_abc",0]' }]),
    ],
    [
      "a malformed question id",
      reply([{ answer: "Blue", questionItemId: '["request_user_input_async",broken' }]),
    ],
    ["no answers", reply([{ answer: "", questionItemId: item(0) }])],
  ])("leaves %s unchanged", (_name, text) => {
    expect(codexAsyncQuestionReplyText(text)).toBe(text);
  });
});
