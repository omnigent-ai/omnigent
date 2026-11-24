// What the workspace-wide text search is called wherever it's offered, so ⌘K, the file
// viewer's menu, the New tab page, the shortcut list, the finder and the Search panel name it alike.

export const WORKSPACE_TEXT_SEARCH_COPY = {
  /** The command, in ⌘K and the file viewer's menu. */
  command: "Search Text in Workspace…",
  /** The shortcut list's description of what the chord does. */
  shortcut: "Search text in the open session's files",
  /** Its row among the New tab page's apps. */
  app: "Search text",
  /** The finder's title. */
  title: "Search Text",
  /** The search field's name, in the finder and the Search panel. */
  field: "Search text in workspace",
  /** The finder's field placeholder. */
  placeholder: "Search the text of this session's files…",
  /** The rail panel that keeps a search's results open beside the conversation. */
  panel: "Search panel",
  /** The Search panel's way back to the finder, with the panel's query. */
  openInFinder: "Open in search dialog",
} as const;
