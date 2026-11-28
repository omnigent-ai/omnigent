// What a pull request comment's ⋯ menu and its ⌘K rows offer, from one set of definitions:
// Show in Changes, Edit, Quote Reply, Add Reaction…, Copy Link, Open in Browser and Delete…, each only where
// it can do something, and the editor's and recovery's own rows in ⌘K.

import {
  CheckIcon,
  CopyIcon,
  ExternalLinkIcon,
  FileDiffIcon,
  LinkIcon,
  PencilIcon,
  QuoteIcon,
  RefreshCwIcon,
  RotateCcwIcon,
  SmilePlusIcon,
  Trash2Icon,
  XIcon,
  type LucideIcon,
} from "lucide-react";
import type { CommentQuoteDestination } from "@/lib/commentQuote";
import { parseHttpUrl } from "@/lib/openLinkInApp";

export type CommentActionId =
  | "show-in-changes"
  | "edit"
  | "quote"
  | "add-reaction"
  | "copy-link"
  | "open"
  | "delete"
  | "cancel-edit"
  | "keep-mine"
  | "load-latest"
  | "check-again"
  | "copy-kept"
  | "discard-kept";

export interface CommentActionDefinition {
  id: CommentActionId;
  /** The ⋯ menu's label, beside the comment it acts on. */
  menuLabel: string;
  /** The ⌘K label, which must name a comment on its own. */
  paletteLabel: string;
  icon: LucideIcon;
  keywords: string[];
  /** The action moves focus (into the reply box), so ⌘K runs it once it has closed. */
  movesFocus?: boolean;
  /** Only in ⌘K: the editor and the recovery strips show these as buttons. */
  paletteOnly?: boolean;
  /** Set apart at the menu's end and styled as destructive. */
  destructive?: boolean;
}

export const COMMENT_ACTIONS: Record<CommentActionId, CommentActionDefinition> = {
  "show-in-changes": {
    id: "show-in-changes",
    menuLabel: "Show in Changes",
    paletteLabel: "Show Conversation in Changes",
    icon: FileDiffIcon,
    keywords: ["conversation", "thread", "changes", "diff", "file", "line", "pull request"],
    movesFocus: true,
  },
  edit: {
    id: "edit",
    menuLabel: "Edit",
    paletteLabel: "Edit Comment",
    icon: PencilIcon,
    keywords: ["comment", "edit", "change", "pull request"],
    movesFocus: true,
  },
  quote: {
    id: "quote",
    menuLabel: "Quote Reply",
    paletteLabel: "Quote Reply",
    icon: QuoteIcon,
    keywords: ["comment", "reply", "quote", "pull request"],
    movesFocus: true,
  },
  "add-reaction": {
    id: "add-reaction",
    menuLabel: "Add Reaction…",
    paletteLabel: "Add Reaction to Comment…",
    icon: SmilePlusIcon,
    keywords: ["comment", "reaction", "react", "emoji", "thumbs up", "pull request"],
    movesFocus: true,
  },
  "copy-link": {
    id: "copy-link",
    menuLabel: "Copy Link",
    paletteLabel: "Copy Comment Link",
    icon: LinkIcon,
    keywords: ["comment", "link", "url", "permalink", "copy", "pull request"],
  },
  open: {
    id: "open",
    menuLabel: "Open in Browser",
    paletteLabel: "Open Comment in Browser",
    icon: ExternalLinkIcon,
    keywords: ["comment", "open", "browser", "github", "external", "pull request"],
  },
  delete: {
    id: "delete",
    menuLabel: "Delete…",
    paletteLabel: "Delete Comment…",
    icon: Trash2Icon,
    keywords: ["comment", "delete", "remove", "pull request"],
    movesFocus: true,
    destructive: true,
  },
  "cancel-edit": {
    id: "cancel-edit",
    menuLabel: "Cancel",
    paletteLabel: "Cancel Comment Edit",
    icon: XIcon,
    keywords: ["comment", "edit", "cancel", "discard", "pull request"],
    movesFocus: true,
    paletteOnly: true,
  },
  "keep-mine": {
    id: "keep-mine",
    menuLabel: "Keep Mine",
    paletteLabel: "Keep My Comment Edit",
    icon: CheckIcon,
    keywords: ["comment", "edit", "conflict", "keep", "mine", "pull request"],
    paletteOnly: true,
  },
  "load-latest": {
    id: "load-latest",
    menuLabel: "Load Latest",
    paletteLabel: "Load Latest Comment",
    icon: RotateCcwIcon,
    keywords: ["comment", "edit", "conflict", "latest", "github", "pull request"],
    paletteOnly: true,
  },
  "check-again": {
    id: "check-again",
    menuLabel: "Check Again",
    paletteLabel: "Check Comment Again",
    icon: RefreshCwIcon,
    keywords: ["comment", "check", "again", "edit", "delete", "github", "pull request"],
    paletteOnly: true,
  },
  "copy-kept": {
    id: "copy-kept",
    menuLabel: "Copy Text",
    paletteLabel: "Copy Unsaved Comment Text",
    icon: CopyIcon,
    keywords: ["comment", "copy", "unsaved", "text", "pull request"],
    paletteOnly: true,
  },
  "discard-kept": {
    id: "discard-kept",
    menuLabel: "Discard",
    paletteLabel: "Discard Unsaved Comment Text",
    icon: Trash2Icon,
    keywords: ["comment", "discard", "unsaved", "text", "pull request"],
    paletteOnly: true,
  },
};

/**
 * A comment's reaction command: Add Reaction… where the viewer may react. The smiley, the ⋯
 * menu and ⌘K all run this same row.
 */
export interface CommentReactionActions {
  addReaction: CommentActionRow | null;
}

/** The reaction row for `openPicker`, only where it can run. */
export function commentReactionActions({
  openPicker,
}: {
  openPicker: (() => void) | null;
}): CommentReactionActions {
  return {
    addReaction: openPicker
      ? { definition: COMMENT_ACTIONS["add-reaction"], run: openPicker }
      : null,
  };
}

/** What a comment's edit and delete offer now, and what they do. */
export interface CommentEditActions {
  /** The viewer's own comment, which this Mac can edit or delete. */
  canEdit: boolean;
  canDelete: boolean;
  /** Why neither can run now, e.g. a change still out on this pull request. */
  blockedReason?: string;
  editing: boolean;
  saving: boolean;
  /** The text in the editor can be saved: non-empty and not too long. */
  saveable: boolean;
  conflict: boolean;
  /** A change GitHub didn't confirm, and whether Check Again can read it now. */
  unconfirmed: boolean;
  canCheck: boolean;
  checking: boolean;
  /** Text kept after the comment was deleted on GitHub. */
  kept: boolean;
  /** Text the person can still copy: kept after a delete, or of an unconfirmed edit. */
  hasKeptText: boolean;
  start: () => void;
  save: () => void;
  cancel: () => void;
  keepMine: () => void;
  loadLatest: () => void;
  askDelete: () => void;
  checkAgain: () => void;
  copyKept: () => void;
  discardKept: () => void;
}

export interface CommentActionTarget {
  /** The comment's own link; null when it has none (a review's body links to the PR). */
  permalink: string | null;
  /** The text a quote takes; null for a hidden or empty comment. */
  quotableBody: string | null;
  /** Null where there's no box to reply in. */
  quoteDestination: CommentQuoteDestination | null;
  copyLink: (url: string) => void;
  openLink: (url: string) => void;
  /** Absent where the comment can't be changed here at all. */
  edit?: CommentEditActions | null;
  /** Opens the comment's conversation in Changes; absent outside a thread, or in Changes. */
  showInChanges?: (() => void) | null;
  /** Absent where the comment's read carries no reactions. */
  reactions?: CommentReactionActions | null;
}

export interface CommentActionRow {
  definition: CommentActionDefinition;
  /** Shown, but not runnable, for this reason. */
  disabledReason?: string;
  run: () => void;
}

/** The rows a comment offers, in menu order; empty means no ⋯ at all. */
export function commentActionRows(target: CommentActionTarget): CommentActionRow[] {
  const rows: CommentActionRow[] = [];
  const { permalink, quotableBody, quoteDestination, edit, showInChanges } = target;
  if (showInChanges)
    rows.push({ definition: COMMENT_ACTIONS["show-in-changes"], run: showInChanges });
  if (edit?.canEdit && !edit.editing && !edit.kept) {
    rows.push({
      definition: COMMENT_ACTIONS.edit,
      disabledReason: edit.blockedReason,
      run: edit.start,
    });
  }
  if (quotableBody !== null && quoteDestination) {
    rows.push({
      definition: COMMENT_ACTIONS.quote,
      disabledReason: quoteDestination.blockedReason,
      run: () => quoteDestination.quote(quotableBody),
    });
  }
  if (target.reactions?.addReaction) rows.push(target.reactions.addReaction);
  if (permalink) {
    rows.push({ definition: COMMENT_ACTIONS["copy-link"], run: () => target.copyLink(permalink) });
    rows.push({ definition: COMMENT_ACTIONS.open, run: () => target.openLink(permalink) });
  }
  if (edit) rows.push(...editRows(edit));
  return rows;
}

/** Delete…, then the rows ⌘K alone offers for the editor and for a change to recover. */
function editRows(edit: CommentEditActions): CommentActionRow[] {
  const rows: CommentActionRow[] = [];
  if (edit.canDelete && !edit.kept) {
    rows.push({
      definition: COMMENT_ACTIONS.delete,
      disabledReason: edit.blockedReason ?? (edit.saving ? "Wait for the edit to save" : undefined),
      run: edit.askDelete,
    });
  }
  // The editor's own ⌘K row saves, as "Save Comment Edit"; Cancel is the card's.
  if (edit.editing && !edit.unconfirmed) {
    rows.push({
      definition: COMMENT_ACTIONS["cancel-edit"],
      disabledReason: edit.saving ? "Wait for the edit to save" : undefined,
      run: edit.cancel,
    });
  }
  if (edit.conflict) {
    rows.push({ definition: COMMENT_ACTIONS["keep-mine"], run: edit.keepMine });
    rows.push({ definition: COMMENT_ACTIONS["load-latest"], run: edit.loadLatest });
  }
  if (edit.unconfirmed && edit.canCheck) {
    rows.push({
      definition: COMMENT_ACTIONS["check-again"],
      disabledReason: edit.checking ? "Checking the comment" : undefined,
      run: edit.checkAgain,
    });
  }
  // Text kept after a delete, or held with an unconfirmed edit, can be copied or dropped.
  if (edit.hasKeptText && (edit.kept || edit.unconfirmed)) {
    rows.push({ definition: COMMENT_ACTIONS["copy-kept"], run: edit.copyKept });
    rows.push({ definition: COMMENT_ACTIONS["discard-kept"], run: edit.discardKept });
  }
  return rows;
}

/** Describes `rows` for a memo: their ids and why any is unavailable. */
export function commentActionRowsKey(rows: CommentActionRow[]): string {
  return rows.map((row) => `${row.definition.id}:${row.disabledReason ?? ""}`).join("\n");
}

/**
 * A comment's own http(s) link. A review's body carries the pull request's link, which isn't
 * the comment's, so it has none.
 */
export function commentPermalink(url: string | null, pullRequestUrl: string | null): string | null {
  if (!url) return null;
  const href = parseHttpUrl(url)?.href ?? null;
  if (!href) return null;
  const pullRequestHref = pullRequestUrl ? parseHttpUrl(pullRequestUrl)?.href : undefined;
  return href === pullRequestHref ? null : href;
}
