/**
 * Focus hand-off from overlay commands (the ⌘K palette) to the page's primary
 * composer. A closing modal dialog settles focus at unmount time — after the
 * destination page has already mounted and claimed focus — so a navigating
 * selection suppresses the dialog's own restore and invokes the registered
 * composer instead. Stack-based so route swaps with overlapping mount/unmount
 * ordering (and StrictMode double-mounts) resolve to the live composer.
 */

const registered: (() => void)[] = [];

/** Register the mounted page's composer focuser; returns an unregister. */
export function registerComposerFocus(focus: () => void): () => void {
  registered.push(focus);
  return () => {
    const at = registered.indexOf(focus);
    if (at !== -1) registered.splice(at, 1);
  };
}

/** Focus the most recently registered composer; false when none is mounted. */
export function focusComposer(): boolean {
  const focus = registered[registered.length - 1];
  if (!focus) return false;
  focus();
  return true;
}
