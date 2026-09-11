/**
 * Codex `/side` command helpers.
 *
 * A `/side` message never joins the conversation it is typed in: the codex
 * executor forks it into a side chat, so the main transcript must not keep an
 * optimistic bubble for it. Kept beside the wire code so the composer, the
 * store, and the server agree on what counts as the command.
 */

/** Prefix that opens a side chat. The trailing space keeps `/sidebar` out. */
export const SIDE_CHAT_COMMAND_PREFIX = "/side ";

/**
 * Whether `text` is a `/side <question>` command.
 *
 * Mirrors `side_chat_question_from_text` in
 * `omnigent/harnesses/codex_native/side_chat.py` — the two must agree, or the
 * text is either stranded in the parent chat or dropped entirely.
 */
export function isSideChatCommand(text: string): boolean {
  if (!text.startsWith(SIDE_CHAT_COMMAND_PREFIX)) return false;
  return text.slice(SIDE_CHAT_COMMAND_PREFIX.length).trim().length > 0;
}
