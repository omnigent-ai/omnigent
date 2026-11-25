// A close from the keyboard takes the focused tab or its content with it. Keep focus in the
// workspace sidebar, on the tab selected next, so the next ⌘W still acts on the sidebar.

import { regionOfFocusTarget } from "../commandContext";
import type { TabId } from "./workspacePlace";

/** Whether keyboard focus is in the workspace sidebar right now. */
export function isFocusInWorkspaceSidebar(): boolean {
  return regionOfFocusTarget(document.activeElement) === "workspace";
}

/**
 * After `closing` settles, focus the selected tab's strip entry when the close came from the
 * sidebar (`fromSidebar`; an embedded page's ⌘W says so though the page's own focus is stale)
 * and focus hasn't settled somewhere on purpose since: in the sidebar, such as a new-tab page
 * that focused its own field, or wherever the user moved it while the close was pending.
 */
export function keepSidebarFocusAfterClose(
  closing: Promise<{ ok: boolean }>,
  selectedTabId: () => TabId | null,
  fromSidebar: boolean = isFocusInWorkspaceSidebar(),
): Promise<void> {
  if (!fromSidebar) return closing.then(() => undefined);
  // A dialog on the way, such as the shell's confirmation, doesn't count as moving away.
  let movedAway = false;
  const onFocusIn = (event: FocusEvent) => {
    const region = regionOfFocusTarget(event.target);
    if (region !== undefined && region !== "workspace") movedAway = true;
  };
  document.addEventListener("focusin", onFocusIn, true);
  const stopWatching = () => document.removeEventListener("focusin", onFocusIn, true);
  return closing.then(
    (result) =>
      new Promise<void>((resolve) => {
        // After the strip has rendered the tab selected next.
        requestAnimationFrame(() => {
          stopWatching();
          const selected = selectedTabId();
          if (result.ok && selected !== null && !movedAway && !isFocusInWorkspaceSidebar()) {
            document.querySelector<HTMLElement>(`[data-workspace-tab-id="${selected}"]`)?.focus();
          }
          resolve();
        });
      }),
    (error: unknown) => {
      stopWatching();
      throw error;
    },
  );
}
