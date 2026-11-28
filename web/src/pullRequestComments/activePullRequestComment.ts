// The one comment ⌘K acts on: the card that last took focus or opened its ⋯ menu. It stays
// while the palette has focus, and goes when that card unmounts or stops being for the PR shown.

import { create } from "zustand";
import type { CommentActionRow } from "./commentActions";

/** Which panel a comment belongs to: the workspace user, the session and the PR it shows. */
export function commentScope(owner: string, conversationId: string, prUrl: string): string {
  return [owner, conversationId, prUrl].join("\n");
}

export interface ActivePullRequestComment {
  /** The card that claimed it; only that card can release it. */
  claim: object;
  scope: string;
  /** Names the comment within its scope, for the ⌘K rows' ids. */
  commentKey: string;
  /** Who wrote it, for the rows' descriptions. */
  author: string | null;
  /** Changes whenever the rows do, so ⌘K rebuilds them. */
  rowsKey: string;
  /** The card's rows as of now; ⌘K runs these, never a copy from an older render. */
  rows: () => CommentActionRow[];
}

interface ActivePullRequestCommentState {
  active: ActivePullRequestComment | null;
  set: (active: ActivePullRequestComment) => void;
  /** Clear it if `claim` still holds it. */
  release: (claim: object) => void;
}

export const useActivePullRequestComment = create<ActivePullRequestCommentState>((set) => ({
  active: null,
  set: (active) => set({ active }),
  release: (claim) => set((state) => (state.active?.claim === claim ? { active: null } : state)),
}));
