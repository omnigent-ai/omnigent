import { describe, expect, it } from "vitest";

import { codexAsyncQuestionReplyText } from "./codexAsyncQuestionReply";

const item = (index: number) => JSON.stringify(["request_user_input_async", "call_abc", index]);
const reply = (entries: unknown) =>
  `<send_user_message_question_reply>\n${JSON.stringify(entries)}\n</send_user_message_question_reply>`;

describe("codexAsyncQuestionReplyText", () => {
  it("shows a reply as the Codex TUI does", () => {
    const text = reply([{ answer: "Blue", question: "Color?", questionItemId: item(0) }]);
    expect(codexAsyncQuestionReplyText(text)).toBe("↪ Re: Color?\nBlue");
  });

  it("shows each answered question of a reply", () => {
    const text = reply([
      { answer: "Blue", question: "Color?", questionItemId: item(0) },
      { answer: "Large", questionItemId: item(1) },
    ]);
    expect(codexAsyncQuestionReplyText(text)).toBe("↪ Re: Color?\nBlue\n\nLarge");
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
    [
      "an entry whose answer is not text",
      reply([
        { answer: "Blue", question: "Color?", questionItemId: item(0) },
        { answer: { text: "Large" }, question: "Size?", questionItemId: item(1) },
      ]),
    ],
    ["no answers", reply([{ answer: "", questionItemId: item(0) }])],
  ])("leaves %s unchanged", (_name, text) => {
    expect(codexAsyncQuestionReplyText(text)).toBe(text);
  });
});
