/**
 * Codex answers a ``request_user_input_async`` question with a user message
 * that holds only a tagged JSON reply, e.g.
 * ``<send_user_message_question_reply>[{"answer":"Blue",...}]</send_user_message_question_reply>``.
 * Omnigent sends its card answers the same way, so the transcript stores the
 * tagged text and the chat shows just the answers.
 */
const REPLY_PATTERN =
  /^\s*<send_user_message_question_reply>\s*([\s\S]*?)\s*<\/send_user_message_question_reply>\s*$/;
const QUESTION_ITEM_PREFIX = '["request_user_input_async",';

/**
 * The answers in a Codex async question reply, or the text unchanged.
 *
 * @param text - A user message's text.
 * @returns One answer per line, e.g. ``"Blue"``, when the whole message is a
 *   well-formed reply; otherwise ``text``.
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
  const answers: string[] = [];
  for (const entry of entries) {
    if (typeof entry !== "object" || entry === null) return text;
    const { answer, questionItemId } = entry as Record<string, unknown>;
    if (typeof questionItemId !== "string" || !questionItemId.startsWith(QUESTION_ITEM_PREFIX)) {
      return text;
    }
    if (typeof answer === "string" && answer) answers.push(answer);
  }
  return answers.length > 0 ? answers.join("\n") : text;
}
