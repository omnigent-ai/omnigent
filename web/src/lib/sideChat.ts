/**
 * `/side` side-chat helpers.
 *
 * A side chat forks the current conversation into its own chat, opened as a
 * soft tab in the Workspace rail — the main transcript is never touched, so a
 * `/side` message must not leave an optimistic bubble behind. Kept beside the
 * wire code so the composer, the store, and the server agree on what counts as
 * the command, and so which harnesses support side chat lives in exactly one
 * place (`supportsSideChat`).
 */

/** Prefix that opens a side chat. The trailing space keeps `/sidebar` out. */
export const SIDE_CHAT_COMMAND_PREFIX = "/side ";

/**
 * Whether a harness supports the `/side` panel side chat.
 *
 * The single onboarding point for side chat across the web app: the composer
 * slash command, the `+` trays, the busy-send bypass, and the text-selection
 * "Ask in side chat" action all gate on this. Side chat is now generic — every
 * harness forks the conversation and continues it in a rail tab — so this is
 * on for all of them. Codex uses its native ephemeral fork
 * (:func:`usesNativeSideChatFork`); the rest fork server-side via
 * `POST /v1/sessions/{id}/side-chat`, degrading gracefully when the host can't
 * drive the fork. To withhold side chat from a future harness, gate it here.
 */
export function supportsSideChat(harness: string | null | undefined): boolean {
  return typeof harness === "string" && harness.length > 0;
}

/**
 * Whether a harness drives its side chat through the *native* Codex ephemeral
 * fork (typed `/side` reaches the runner as plaintext, which forks in-process
 * and stays prompt-cache-warm) rather than the generic server-side fork.
 *
 * The two paths diverge only in how the child is created; both surface it as a
 * side-chat rail tab. Gated in one place so the composer's send routing and the
 * store's optimistic-bubble suppression can't disagree.
 */
export function usesNativeSideChatFork(harness: string | null | undefined): boolean {
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
