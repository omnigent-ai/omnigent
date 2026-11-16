import { settingsEntry } from "./types";

/** How long an archived session is kept before it's marked expired: the select's choices. */
export const ARCHIVED_RETENTION_OPTIONS: readonly {
  label: string;
  value: string;
  days: number | null;
}[] = [
  { label: "Never", value: "never", days: null },
  { label: "After 7 days", value: "7", days: 7 },
  { label: "After 30 days", value: "30", days: 30 },
  { label: "After 60 days", value: "60", days: 60 },
  { label: "After 90 days", value: "90", days: 90 },
];

/** "Delete 2 sessions", or "Delete 1 session". */
const DELETE_SESSIONS = {
  before: "Delete ",
  after: " sessions",
  variants: { one: { before: "Delete ", after: " session" } },
} as const;

export const ARCHIVED_SESSIONS_ENTRY = settingsEntry({
  id: "archived-sessions",
  section: "archived",
  label: "Archived sessions",
  description: "Restore archived sessions to the sidebar or delete them permanently.",
  keywords: [
    "archive",
    "archived",
    "unarchive",
    "restore",
    "delete",
    "expired",
    "retention",
    "cleanup",
    "project",
    "select",
  ],
  parts: {
    retention: "Mark archived sessions as expired after",
    projectFilter: "Filter archived sessions by project",
    select: "Select",
    selectAll: "Select all",
    exitSelection: "Exit selection mode",
    bulkUnarchive: "Unarchive",
    bulkDelete: "Delete",
    bulkConfirm: "Delete sessions",
    deleteExpired: "Delete expired",
    expiredConfirm: "Delete expired sessions",
    loadMore: "Load more",
  },
  partChoices: { retention: ARCHIVED_RETENTION_OPTIONS.map((option) => option.label) },
  // The selection bar replaces Select while choosing; the rest show only when they apply.
  presence: {
    projectFilter: { states: ["has projects"] },
    select: { states: ["browsing"] },
    selectAll: { states: ["selecting"] },
    exitSelection: { states: ["selecting"] },
    bulkUnarchive: { states: ["selecting"] },
    bulkDelete: { states: ["selecting"] },
    bulkConfirm: { overlay: "bulkDelete" },
    deleteExpired: { states: ["expired sessions"] },
    expiredConfirm: { overlay: "deleteExpired" },
    loadMore: { states: ["more pages"] },
  },
  nameAlternatives: {
    selectAll: { deselect: "Deselect all" },
    loadMore: { loading: "Loading…" },
  },
  // Counts of the sessions each acts on; only the static names are searchable.
  nameTemplates: {
    bulkUnarchive: { before: "Unarchive " },
    bulkDelete: { before: "Delete " },
    // Worded as the sidebar's own bulk delete is.
    bulkConfirm: { before: "Delete ", after: " session(s)" },
    expiredConfirm: DELETE_SESSIONS,
  },
  overlays: {
    retention: {},
    // The projects that have archived sessions, after All projects.
    projectFilter: { runtimeChoices: { fixed: ["All projects"] } },
    bulkDelete: { dialogActions: ["Cancel"], captured: true },
    deleteExpired: { dialogActions: ["Cancel"], captured: true },
  },
  // Each archived session's own Delete and Unarchive, and the confirmation Delete opens.
  regions: {
    sessions: {
      rowActions: { delete: "Delete session", unarchive: "Unarchive" },
      rowOverlays: { delete: { rowActions: { cancel: "Cancel", delete: "Delete" } } },
    },
  },
  focus: "retention",
});

export const ARCHIVED_SETTINGS = [ARCHIVED_SESSIONS_ENTRY] as const;
