// The ids of a session's actions as ⌘K offers them. Kept apart from their model, so the palette's
// pages and targets can name an action without loading it.

/** Every action, in the row menu's order. Toggles are two ids, so a stale row can't flip back. */
export const SESSION_COMMAND_IDS = [
  "add-sibling",
  "open-in-new-window",
  "pin",
  "unpin",
  "share",
  "fork",
  "rename",
  "mute",
  "unmute",
  "mark-unread",
  "mark-read",
  "create-automation",
  "move-to-project",
  "move-to-nest",
  "stop",
  "archive",
  "unarchive",
  "delete",
  "leave",
] as const;

export type SessionCommandId = (typeof SESSION_COMMAND_IDS)[number];
