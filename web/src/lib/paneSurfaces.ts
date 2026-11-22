// Where the panes view's surfaces sit in the DOM, for focus and key routing: a pane's terminal,
// the web chat as a pane, and a workspace tab shown as a pane.

/** Data attribute every terminal container in a pane carries. */
export const TERMINAL_SURFACE_ATTRIBUTE = "data-terminal-surface";

/** A focused terminal: it forwards its keys to the shell, so app chords defer to it. */
export const TERMINAL_SURFACE = `[${TERMINAL_SURFACE_ATTRIBUTE}]`;

/** A pane's tile in a split; its `data-terminal-pane` value is the pane's key. */
export const TERMINAL_PANE = "[data-terminal-pane]";

/**
 * The web chat as a pane of the session's split: where it renders, in or out of the split. Its
 * `data-chat-pane-body` value is the session's id, since a grid shows several sessions' chats.
 */
export const CHAT_PANE_BODY = "[data-chat-pane-body]";
export const CHAT_PANE_BODY_ATTRIBUTE = "data-chat-pane-body";

/** Marks an app pane's body, where its tab renders; its value is the tab's id. */
export const APP_PANE_HOST_ATTRIBUTE = "data-app-pane-host";
export const APP_PANE_HOST = `[${APP_PANE_HOST_ATTRIBUTE}]`;
