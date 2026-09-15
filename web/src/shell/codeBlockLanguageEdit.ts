// Shared contract for persisting a code-block language change made from the
// node-view picker. Lives in its own module so the node view and the editor's
// autosave wiring can both import it without a cycle through the extension.

import type { Transaction } from "@tiptap/pm/state";

// Marks the transaction a node-view language change dispatches. The native
// <select> holds focus, so that update reaches the editor while it is blurred;
// the flag lets the autosave/dirty wiring treat it as a real user edit instead
// of a load-time re-baseline (which would silently drop the change on reload).
export const CODE_BLOCK_LANGUAGE_EDIT_META = "codeBlockLanguageEdit";

/**
 * Whether a doc update should count as a user edit rather than a load-time
 * re-baseline. True when the editor is focused, or when the update carries the
 * language-edit flag — picker changes fire while the editor is blurred, and
 * focusing programmatically is unreliable (TipTap defers focus to a frame).
 */
export function isUserEditUpdate(isFocused: boolean, tr: Transaction | undefined | null): boolean {
  return isFocused || tr?.getMeta(CODE_BLOCK_LANGUAGE_EDIT_META) === true;
}
