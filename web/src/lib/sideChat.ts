/**
 * `/side` side-chat helpers.
 *
 * A `/side` message never joins the conversation it is typed in: the harness
 * forks it into a side chat, so the main transcript must not keep an optimistic
 * bubble for it. Kept beside the wire code so the composer, the store, and the
 * server agree on what counts as the command — and so which harnesses support
 * side chat lives in exactly one place (`supportsSideChat`).
 */

/** Prefix that opens a side chat. The trailing space keeps `/sidebar` out. */
export const SIDE_CHAT_COMMAND_PREFIX = "/side ";

/**
 * Whether a harness supports the `/side` side chat.
 *
 * The single onboarding point for side chat across the web app: the composer
 * slash command, the busy-send bypass, and the text-selection "Ask in side
 * chat" action all gate on this. To give a future harness the same capability,
 * add it here — nothing else needs to change.
 */
export function supportsSideChat(harness: string | null | undefined): boolean {
  return harness === "codex-native";
}

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
