// Per-device choice to soft-wrap long lines in chat code blocks and tool-call
// code panels. One toggle drives every block in every session and survives a
// refresh; the file editor's "Wrap lines" setting is separate.

import { createStoredChoicePreference } from "./storedChoicePreference";

export const CODE_BLOCK_WORD_WRAP_STORAGE_KEY = "omnigent:code-block-word-wrap";

export const DEFAULT_CODE_BLOCK_WORD_WRAP = true;

const preference = createStoredChoicePreference(
  CODE_BLOCK_WORD_WRAP_STORAGE_KEY,
  ["false", "true"],
  DEFAULT_CODE_BLOCK_WORD_WRAP ? "true" : "false",
);

/** Read the preference; storage errors and unknown values mean the default. */
export function readCodeBlockWordWrap(): boolean {
  return preference.read() === "true";
}

/** Persist the preference and re-render every code block on screen. */
export function writeCodeBlockWordWrap(wrap: boolean): void {
  preference.write(wrap ? "true" : "false");
}

/** Flip the preference from its current stored value. */
export function toggleCodeBlockWordWrap(): void {
  writeCodeBlockWordWrap(!readCodeBlockWordWrap());
}

/** Re-read after a same-tab write that bypassed `writeCodeBlockWordWrap` (settings import). */
export const notifyCodeBlockWordWrapChanged = preference.notifyChanged;

/** Live value of the preference, including changes made in other windows. */
export function useCodeBlockWordWrap(): boolean {
  return preference.use() === "true";
}
