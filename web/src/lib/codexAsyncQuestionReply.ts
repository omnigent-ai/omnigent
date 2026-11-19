const REPLY_PATTERN =
  /^\s*<send_user_message_question_reply>\s*([\s\S]*?)\s*<\/send_user_message_question_reply>\s*$/;

/** Whether ``id`` names an async question, e.g. ``'["request_user_input_async","call_abc",0]'``. */
function isAsyncQuestionItemId(id: unknown): boolean {
  if (typeof id !== "string") return false;
  let parts: unknown;
  try {
    parts = JSON.parse(id);
  } catch {
    return false;
  }
  return (
    Array.isArray(parts) &&
    parts.length === 3 &&
    parts[0] === "request_user_input_async" &&
    typeof parts[1] === "string" &&
    parts[1] !== "" &&
    Number.isInteger(parts[2])
  );
}

/**
 * A Codex async question reply shown as the Codex TUI shows it, or the text
 * unchanged. The TUI picker and Omnigent's question card both reply with a
 * user message holding only
 * ``<send_user_message_question_reply>[{"answer":"Blue",...}]</send_user_message_question_reply>``.
 *
 * @param text - A user message's text.
 * @returns ``"↪ Re: <question>\n<answer>"`` per answered question, e.g.
 *   ``"↪ Re: Which color?\nBlue"``, when the whole message is a well-formed
 *   reply; otherwise ``text``.
 */
export function codexAsyncQuestionReplyText(text: string): string {
  const match = REPLY_PATTERN.exec(text);
  if (!match) return text;
  let entries: unknown;
  try {
    entries = JSON.parse(match[1]);
  } catch {
    return text;
  }
  if (!Array.isArray(entries) || entries.length === 0) return text;
  const replies: string[] = [];
  for (const entry of entries) {
    if (typeof entry !== "object" || entry === null) return text;
    const { answer, question, questionItemId } = entry as Record<string, unknown>;
    if (!isAsyncQuestionItemId(questionItemId)) return text;
    if (typeof answer !== "string") return text;
    if (question !== undefined && typeof question !== "string") return text;
    if (!answer) continue;
    replies.push(
      typeof question === "string" && question ? `↪ Re: ${question}\n${answer}` : answer,
    );
  }
  return replies.length > 0 ? replies.join("\n\n") : text;
}
