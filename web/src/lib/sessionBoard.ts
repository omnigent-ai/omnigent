// Pure helpers for the session Board page (`/board`): place each session row in
// a work-state column. Kept free of React and store imports so the column rules
// are unit-testable on their own.

import type { Conversation } from "@/hooks/useConversations";
import { getSessionState, type SessionState } from "@/hooks/useSessionState";
import { latestErrorFromLabels } from "@/lib/sessionError";

/**
 * Session label recording the user's acceptance of a session's work. The client
 * sends the `board_revision` it reviewed; the server stores a stamp one second
 * past `updated_at` if that revision is still current, and every later write
 * that advances `updated_at` deletes the label, so new activity reopens it.
 */
export const BOARD_ACCEPTED_LABEL_KEY = "omnigent.board.accepted";

export type BoardColumnId = "running" | "needs_you" | "needs_review" | "done";

export interface BoardColumn {
  id: BoardColumnId;
  title: string;
  /** Shown when the column has no cards. */
  empty: string;
}

/** Columns in the order the board renders them. */
export const BOARD_COLUMNS: readonly BoardColumn[] = [
  { id: "running", title: "Running", empty: "No agents are working." },
  { id: "needs_you", title: "Needs you", empty: "Nothing is waiting on you." },
  { id: "needs_review", title: "Needs review", empty: "Nothing to review." },
  { id: "done", title: "Done", empty: "Accepted sessions appear here." },
];

/**
 * The server's acceptance stamp, or null when the session was never accepted
 * (or the label was cleared to an empty value).
 */
export function boardAcceptedAt(row: Pick<Conversation, "labels">): number | null {
  const raw = row.labels?.[BOARD_ACCEPTED_LABEL_KEY];
  if (!raw) return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

/** Whether the user accepted the session and nothing has happened since. */
export function isBoardAccepted(row: Pick<Conversation, "labels" | "updated_at">): boolean {
  const acceptedAt = boardAcceptedAt(row);
  return acceptedAt !== null && acceptedAt > row.updated_at;
}

/**
 * The row's badge state, including the persisted latest-activity error the
 * sidebar also reads, so both surfaces flag the same sessions.
 */
export function boardSessionState(row: Conversation): SessionState | null {
  return getSessionState(row, latestErrorFromLabels(row.labels, row.host_online));
}

/**
 * The column a session belongs in. Waiting on the user and running work win
 * over acceptance; a failed turn, errored activity or a disconnected host needs
 * the user until it is accepted; any other idle session needs review until the
 * user accepts it.
 */
export function boardColumn(row: Conversation): BoardColumnId {
  const state = boardSessionState(row);
  if (state?.kind === "awaiting") return "needs_you";
  if (state?.kind === "running") return "running";
  if (isBoardAccepted(row)) return "done";
  if (state?.kind === "error" || state?.kind === "disconnected") return "needs_you";
  return "needs_review";
}

/** Whether a card offers Accept: the session is idle and not already accepted. */
export function canAcceptOnBoard(row: Conversation): boolean {
  const column = boardColumn(row);
  return (
    column === "needs_review" ||
    (column === "needs_you" && boardSessionState(row)?.kind === "error")
  );
}

/**
 * Split live session rows into board columns, newest activity first. Archived
 * rows and client-only placeholders for sessions still being created are left
 * out.
 */
export function groupBoardColumns(
  rows: readonly Conversation[],
): Record<BoardColumnId, Conversation[]> {
  const columns: Record<BoardColumnId, Conversation[]> = {
    running: [],
    needs_you: [],
    needs_review: [],
    done: [],
  };
  const newestFirst = rows
    .filter((row) => !row.archived && !row.provisional)
    .sort((a, b) => b.updated_at - a.updated_at);
  for (const row of newestFirst) columns[boardColumn(row)].push(row);
  return columns;
}
