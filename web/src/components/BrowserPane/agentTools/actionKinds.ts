// Which agent browser actions do what, for the relay. Kept apart from
// relayActions.ts so the relay can decide before that loads.
import { desktopSupportsBrowserAgentTools } from "./preferences";

// Back / forward / reload: main checks where they land against the issuing
// session's host, as it does a navigation.
export const HISTORY_ACTIONS = new Set(["go_back", "go_forward", "reload"]);

// Input that can navigate the page (a link, a form, Enter). Main locks the
// tab to the issuing session's URL policy for it, so these carry the
// session's host too.
export const INPUT_ACTIONS = new Set([
  "click",
  "type",
  "hover",
  "drag",
  "scroll",
  "press_key",
  "select_option",
  "check",
]);

/** Whether an action can open a URL, so the relay establishes its provenance. */
export function navigatesAgentBrowser(action: string): boolean {
  return (
    action === "navigate" ||
    action === "tabs" ||
    HISTORY_ACTIONS.has(action) ||
    // Only apps with the tools take the host with input (see dispatch).
    (INPUT_ACTIONS.has(action) && desktopSupportsBrowserAgentTools())
  );
}
