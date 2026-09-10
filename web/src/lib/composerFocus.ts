/**
 * Focus hand-off from overlay commands (the ⌘K palette) to the page's primary
 * composer. A closing modal dialog settles focus at unmount time — after the
 * destination page has already mounted and claimed focus — so a navigating
 * selection suppresses the dialog's own restore and invokes the registered
 * composer instead. Stack-based so route swaps with overlapping mount/unmount
 * ordering (and StrictMode double-mounts) resolve to the live composer.
 */

/** Focuses the page's composer, reporting whether focus actually moved
    (false on mobile's tap-to-focus surfaces or when the textarea is gone). */
export type ComposerFocus = () => boolean;

const registered: ComposerFocus[] = [];

/** Register the mounted page's composer focuser; returns an unregister. */
export function registerComposerFocus(focus: ComposerFocus): () => void {
  registered.push(focus);
  return () => {
    const at = registered.indexOf(focus);
    if (at !== -1) registered.splice(at, 1);
  };
}

/** Ask the most recently registered composer to take focus. False when none
    is mounted or it didn't take focus — the caller must then keep its own
    default focus handling instead of leaving focus stranded. */
export function focusComposer(): boolean {
  const focus = registered[registered.length - 1];
  return focus ? focus() : false;
}
