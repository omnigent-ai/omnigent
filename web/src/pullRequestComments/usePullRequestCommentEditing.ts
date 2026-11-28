// One comment card's Edit and Delete: whether the session's host offers them for it, and what
// they do, through the comment's entry in the edit store, so every card showing it agrees.

import { randomUUID } from "@/lib/randomUUID";
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  type PullRequestCommentEditChange,
  usePullRequestCommentEditWrite,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { copyText } from "@/lib/clipboard";
import type { CommentChangeOperation } from "@/lib/pullRequestStateHolds";
import { PullRequestWriteError } from "@/lib/pullRequestWriteAnswers";
import { useWorkspaceDraftOwner } from "@/lib/workspaceDraftOwner";
import { parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import {
  type HostChangeRoute,
  hostChangeRoute,
  hostChangeRouteCurrent,
  usePullRequestMethodServed,
} from "@/pullRequestPage/pullRequestPageRoute";
import type { Method } from "@/lib/workspaceApi";
import { HOST_ACTOR } from "@/pullRequestPage/usePullRequestStateAction";
import { hostPendingIdentity } from "@/pullRequestReviews/hostPendingRoute";
import {
  PENDING_REVIEW_END_UNRESOLVED,
  pendingReviewEndUnresolved,
} from "@/pullRequestReviews/pendingReviewChanges";
import type { CommentEditActions } from "./commentActions";
import { checkCommentChange, commentCheckReady } from "./commentChangeCheck";
import { commentEditVisible } from "./commentEditScope";
import { commentChangeCurrent, watchCommentChange } from "./commentChangeRetirement";
import {
  type CommentChange,
  type CommentCheckTarget,
  type CommentEditEntry,
  EMPTY_ENTRY,
  changeConflicted,
  changeFoundDeleted,
  changeMade,
  changeRefused,
  changeSent,
  changeUncertain,
  commentChangeHeld,
  commentEditKey,
  editDirty,
  discardLocalText,
  editText,
  keepMine,
  keptText,
  loadLatest,
  readCommentEdit,
  saveBlocked,
  startEditing,
  stopEditing,
  updateCommentEdit,
  useCommentEdit,
} from "./commentEditStore";

/** The comment as its card shows it. */
export interface CommentEditSubject {
  conversationId: string;
  /** The PR the panel shows, as its reads and drafts name it. */
  prUrl: string;
  /** GitHub's node id. */
  commentId: string | undefined;
  /** GitHub's number for it, which the session's host changes it by; null where unread. */
  commentNumber?: number | null;
  /** Present for a review comment: the conversation it's in. */
  threadId?: string;
  body: string;
  canUpdate?: boolean;
  canDelete?: boolean;
  /** The host's account wrote it, from the host's read. */
  viewerDidAuthor?: boolean;
  /** Present for a draft: the pending review it's in, whose draft changes it takes. */
  pendingReviewId?: string;
}

export interface CommentEditing {
  actions: CommentEditActions | null;
  entry: CommentEditEntry;
  /** The editor's text, and how to change it. */
  setText: (text: string) => void;
  /** Delete's confirmation, opened by Delete… and answered by its dialog. */
  deleteAsked: boolean;
  setDeleteAsked: (open: boolean) => void;
  confirmDelete: () => void;
  /** Leaving an editor with changed text asks first. */
  discardAsked: boolean;
  setDiscardAsked: (open: boolean) => void;
  confirmDiscard: () => void;
}

const OPERATIONS: Record<CommentChange, CommentChangeOperation> = {
  edit: "pull_request_comment_edit",
  delete: "pull_request_comment_delete",
};
// A draft in the pending review changes through the review, never as a published comment.
const DRAFT_OPERATIONS: Record<CommentChange, CommentChangeOperation> = {
  edit: "pull_request_review_draft_edit",
  delete: "pull_request_review_draft_delete",
};

// The session's host changes a published comment by these, by its number.
const HOST_METHODS: Record<CommentChange, Method> = {
  edit: "editComment",
  delete: "deleteComment",
};
// And a draft by these, in the pending review the page saw: Edit an editor's, Delete the owner's.
const HOST_DRAFT_METHODS: Record<CommentChange, Method> = {
  edit: "updatePendingReviewComment",
  delete: "deletePendingReviewComment",
};

function newRequestId(): string {
  return randomUUID();
}

/**
 * A card's Edit and Delete for `subject`. `onDeleteConfirmed` hears of a delete as it's sent,
 * while the card is still there; `onDeleted` once GitHub made it, so focus can move on.
 */
export function usePullRequestCommentEditing(
  subject: CommentEditSubject,
  { onDeleteConfirmed, onDeleted }: { onDeleteConfirmed?: () => void; onDeleted?: () => void } = {},
): CommentEditing {
  const queryClient = useQueryClient();
  const owner = useWorkspaceDraftOwner();
  const { conversationId, prUrl, commentId, threadId, pendingReviewId } = subject;
  const operations = pendingReviewId ? DRAFT_OPERATIONS : OPERATIONS;
  // A draft is private to the host route it was read on: its edit keeps that scope with it.
  const privateScopeOf = (host: HostChangeRoute) =>
    pendingReviewId
      ? {
          privateScope: {
            login: HOST_ACTOR.login,
            reader: null,
            host: { conversationId: host.conversationId, key: host.key },
          },
        }
      : {};
  const key = commentId ? commentEditKey(owner, prUrl, commentId) : null;
  const keptEntry = useCommentEdit(key);
  // Another route's private draft text under this comment stays theirs: nothing of it shows
  // here, and nothing here may change it, until that route is back.
  const othersPrivate = !commentEditVisible(keptEntry, prUrl, conversationId);
  const entry = othersPrivate ? EMPTY_ENTRY : keptEntry;
  const editWrite = usePullRequestCommentEditWrite(conversationId, operations.edit);
  const deleteWrite = usePullRequestCommentEditWrite(conversationId, operations.delete);
  // Whether Delete… was asked; the dialog's confirmation sends the delete.
  const [deleteAsked, setDeleteAsked] = useState(false);
  const [discardAsked, setDiscardAsked] = useState(false);

  // The session's host changes a published comment by its number, and a draft in its pending
  // review, where it serves the method for this caller.
  const hostMethods = pendingReviewId ? HOST_DRAFT_METHODS : HOST_METHODS;
  const hostEdits = usePullRequestMethodServed(conversationId, hostMethods.edit);
  const hostDeletes = usePullRequestMethodServed(conversationId, hostMethods.delete);
  const commentNumber = subject.commentNumber ?? null;
  const hostRouteFor = (change: CommentChange): HostChangeRoute | null =>
    pendingReviewId || commentNumber !== null
      ? hostChangeRoute(conversationId, hostMethods[change])
      : null;
  // GitHub lets a repository admin edit anyone's comment; through the host, only the owner
  // (whose list has the OWNER-level DeleteComment) edits one the host's account didn't write.
  // A draft is always the host account's own.
  const hostEditsThis =
    hostEdits && (!!pendingReviewId || subject.viewerDidAuthor === true || hostDeletes);
  const offers = (change: CommentChange) =>
    (change === "edit" ? hostEditsThis : hostDeletes) && hostRouteFor(change) !== null;
  const canEdit = !othersPrivate && !!commentId && subject.canUpdate === true && offers("edit");
  const canDelete = !othersPrivate && !!commentId && subject.canDelete === true && offers("delete");
  const shown = canEdit || canDelete || entry !== EMPTY_ENTRY;
  const update = (change: (current: CommentEditEntry) => CommentEditEntry) => {
    if (key && commentEditVisible(readCommentEdit(key), prUrl, conversationId)) {
      updateCommentEdit(key, change);
    }
  };

  const blockedReason = entry.pending
    ? entry.pending.change === "edit"
      ? "Wait for the edit to save"
      : "Wait for the comment to be deleted"
    : entry.unknown
      ? "Check this comment before changing it again"
      : undefined;

  /**
   * Send `change` now, from the click, with everything it needs captured. Nothing is sent
   * while a change to this comment is out or unconfirmed.
   */
  const send = (
    change: CommentChange,
    payload: Pick<PullRequestCommentEditChange, "body" | "expected">,
  ) => {
    if (!key || !commentId) return;
    const current = readCommentEdit(key);
    if (commentChangeHeld(current) || !commentEditVisible(current, prUrl, conversationId)) return;
    // Decided again now: the route the render saw may have changed since.
    const host = hostRouteFor(change);
    const hostTarget = host ? parseCodeHostPullRequestUrl(prUrl) : null;
    if (!host || !hostTarget || !hostChangeRouteCurrent(host, hostMethods[change])) {
      updateCommentEdit(key, (stored) => ({
        ...stored,
        message: "Edit and delete this comment on GitHub.",
      }));
      return;
    }
    // A draft's review being submitted or discarded, or left unconfirmed, takes no change to it.
    if (pendingReviewId && pendingReviewEndUnresolved(owner, prUrl, hostPendingIdentity(host))) {
      updateCommentEdit(key, (stored) => ({ ...stored, message: PENDING_REVIEW_END_UNRESOLVED }));
      return;
    }
    const requestId = newRequestId();
    const kind = threadId ? "review" : "issue";
    const check: CommentCheckTarget = {
      prUrl,
      repository: hostTarget.repository,
      number: hostTarget.number,
      commentId,
      kind,
      login: HOST_ACTOR.login,
      host,
      threadId,
      ...(pendingReviewId ? { pendingReviewId } : {}),
    };
    updateCommentEdit(key, (stored) => ({
      ...changeSent(stored, change, requestId),
      kind,
      ...privateScopeOf(host),
    }));
    const write = change === "edit" ? editWrite : deleteWrite;
    const held = { sent: payload.body, expected: payload.expected, check };
    // Retired into the held state if the host's route changes before GitHub answers.
    const settle = watchCommentChange({
      key,
      requestId,
      prUrl,
      operation: operations[change],
      held,
      hostMethod: hostMethods[change],
    });
    // The key as of the click: an answer settles that workspace user's entry, never another's.
    const uncertain = () =>
      updateCommentEdit(key, (stored) => changeUncertain(stored, requestId, held));
    write.mutate(
      {
        prUrl,
        commentId,
        threadId,
        pendingReviewId,
        commentNumber,
        ...payload,
        sentAt: Date.now(),
        stillCurrent: () => commentChangeCurrent(requestId),
      },
      {
        onSuccess: () => {
          // A change that retired meanwhile waits for Check Again; its late answer is ignored.
          if (!settle()) return;
          if (change === "delete") onDeleted?.();
          updateCommentEdit(key, (stored) => changeMade(stored, requestId));
        },
        onError: (error) => {
          if (!settle()) return;
          if (!(error instanceof PullRequestWriteError)) {
            updateCommentEdit(key, (stored) => changeRefused(stored, requestId, error.message));
          } else if (error.outcomeUnknown) uncertain();
          else if (error.code === "comment_changed" && error.current?.field === "body") {
            const theirs = error.current.value;
            updateCommentEdit(key, (stored) => changeConflicted(stored, requestId, theirs));
          } else if (error.code === "comment_deleted") {
            updateCommentEdit(key, (stored) =>
              changeFoundDeleted(stored, requestId, error.message),
            );
          } else {
            updateCommentEdit(key, (stored) => changeRefused(stored, requestId, error.message));
          }
        },
      },
    );
  };

  // One rule decides whether the edit can be saved, for the button, ⌘↵ and ⌘K alike.
  const saveReason = saveBlocked(entry);
  const save = () => {
    if (!key) return;
    const current = readCommentEdit(key);
    if (!current.editing || saveBlocked(current)) return;
    send("edit", { body: current.editing.text, expected: current.editing.expected });
  };

  const unknown = entry.unknown;
  const canCheck = commentCheckReady(entry);
  const kept = keptText(entry);
  const copyKept = () => {
    if (kept === null) return;
    void copyText(kept).then(
      () => toast.success("Text copied"),
      () => toast.error("Couldn’t copy the text"),
    );
  };

  const actions: CommentEditActions | null = shown
    ? {
        canEdit,
        canDelete,
        blockedReason,
        editing: entry.editing !== null,
        saving: entry.pending?.change === "edit",
        saveable: saveReason === null,
        conflict: entry.conflict !== null,
        unconfirmed: unknown !== null,
        canCheck,
        checking: entry.checking !== null,
        kept: entry.tombstone !== null,
        hasKeptText: kept !== null,
        start: () => {
          if (!canEdit || blockedReason) return;
          // A draft's text is this session's from the first keystroke, not only once it's sent.
          const host = hostRouteFor("edit");
          if (pendingReviewId && !host) return;
          update((current) => ({
            ...startEditing(current, subject.body),
            kind: threadId ? "review" : "issue",
            ...(host ? privateScopeOf(host) : {}),
          }));
        },
        save,
        cancel: () => {
          if (entry.pending) return;
          if (editDirty(entry)) setDiscardAsked(true);
          else update(stopEditing);
        },
        keepMine: () => update(keepMine),
        loadLatest: () => update(loadLatest),
        askDelete: () => {
          if (!canDelete || blockedReason) return;
          setDeleteAsked(true);
        },
        checkAgain: () => {
          if (key) checkCommentChange(queryClient, conversationId, key);
        },
        copyKept,
        // Dropping the edit of an unconfirmed change asks first; the change stays held.
        discardKept: () => {
          if (entry.unknown) setDiscardAsked(true);
          else update(discardLocalText);
        },
      }
    : null;

  return {
    actions,
    entry,
    setText: (text) => update((current) => editText(current, text)),
    deleteAsked,
    setDeleteAsked,
    confirmDelete: () => {
      setDeleteAsked(false);
      if (!canDelete) return;
      onDeleteConfirmed?.();
      send("delete", {});
    },
    discardAsked,
    setDiscardAsked,
    confirmDiscard: () => {
      setDiscardAsked(false);
      update((current) => (current.unknown ? discardLocalText(current) : stopEditing(current)));
    },
  };
}
