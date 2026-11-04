/** Spread on an inline rename field so a background composer focus leaves it alone. */
export const INLINE_RENAME_FIELD_PROPS = { "data-inline-rename": "" } as const;

/** True while the user is typing a session or grid name in an inline rename field. */
export function isInlineRenameFocused(): boolean {
  return Boolean(document.activeElement?.closest("[data-inline-rename]"));
}
