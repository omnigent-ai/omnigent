// A pull request comment's actions: the ⋯ menu on its card, ⌘-click on its time, and the ⌘K
// rows for the comment last focused, all built from the same rows.

import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type MouseEvent,
  type RefObject,
} from "react";
import { EllipsisIcon } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  type PullRequestComment,
  usePullRequestCommentQuote,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { useMenuCloseHandOff } from "@/hooks/useMenuCloseHandOff";
import { copyText } from "@/lib/clipboard";
import { hasCommandModifier } from "@/lib/hotkeys";
import { openLink } from "@/lib/openLinkInApp";
import { useWorkspaceDraftOwner } from "@/lib/workspaceDraftOwner";
import type { ReviewComment, ReviewThread } from "@/lib/pullRequestReviewThreads";
import { cn } from "@/lib/utils";
import type { ReviewThreadContext } from "@/newPullRequestPage/shell/PullRequestReviewThread";
import {
  type PaletteCommand,
  unavailableBecause,
  usePaletteCommands,
} from "@/store/paletteCommands";
import { commentScope, useActivePullRequestComment } from "./activePullRequestComment";
import type { CommentQuoteDestination } from "@/lib/commentQuote";
import {
  type CommentActionRow,
  type CommentEditActions,
  type CommentReactionActions,
  commentActionRows,
  commentReactionActions,
  commentActionRowsKey,
  commentPermalink,
} from "./commentActions";
import { type CommentEditing, usePullRequestCommentEditing } from "./usePullRequestCommentEditing";
import { type CommentReactionsModel, useCommentReactions } from "./useCommentReactions";
import type { CommentReaction } from "@/lib/pullRequestCommentReactions";

const NO_COMMANDS: PaletteCommand[] = [];

export interface PullRequestCommentActionsInput {
  conversationId: string;
  /** The PR the panel shows, as its drafts name it. */
  prUrl: string;
  /** Names the comment within the PR. */
  commentKey: string;
  author: string | null;
  /** From `commentPermalink`: null where the comment has no link of its own. */
  permalink: string | null;
  /** Null for a hidden or empty comment. */
  quotableBody: string | null;
  quoteDestination: CommentQuoteDestination | null;
  /** Edit and Delete, where this Mac can make them for the viewer's own comment. */
  edit?: CommentEditActions | null;
  /** Opens the comment's conversation in Changes, where the page offers it. */
  showInChanges?: (() => void) | null;
  /** Add Reaction… and Check Again, where the comment's read carries reactions. */
  reactions?: CommentReactionActions | null;
}

export interface PullRequestCommentActions {
  rows: CommentActionRow[];
  /** Make this the comment ⌘K acts on; the card calls it on focus and when its menu opens. */
  claim: () => void;
  /** ⌘-click (Ctrl-click off macOS) on the comment's time opens it; a plain click does nothing. */
  onTimeClick: (event: MouseEvent) => void;
}

function copyCommentLink(url: string) {
  void copyText(url).then(
    () => toast.success("Link copied"),
    () => toast.error("Couldn’t copy the link"),
  );
}

/** One card's actions, and its claim on ⌘K, released when it unmounts or leaves its PR. */
export function usePullRequestCommentActions(
  input: PullRequestCommentActionsInput,
): PullRequestCommentActions {
  const owner = useWorkspaceDraftOwner();
  const scope = commentScope(owner, input.conversationId, input.prUrl);
  const { conversationId, permalink } = input;
  const rows = commentActionRows({
    permalink,
    quotableBody: input.quotableBody,
    quoteDestination: input.quoteDestination,
    edit: input.edit,
    showInChanges: input.showInChanges,
    reactions: input.reactions,
    copyLink: copyCommentLink,
    openLink: (url) => openLink(conversationId, url),
  });
  const rowsKey = commentActionRowsKey(rows);
  const latestRows = useRef(rows);
  latestRows.current = rows;
  const claimToken = useRef({}).current;
  const setActive = useActivePullRequestComment((state) => state.set);
  const release = useActivePullRequestComment((state) => state.release);
  const holds = useActivePullRequestComment((state) => state.active?.claim === claimToken);
  const describe = () => ({
    claim: claimToken,
    scope,
    commentKey: input.commentKey,
    author: input.author,
    rowsKey,
    rows: () => latestRows.current,
  });
  const claim = () => {
    // Focus moving within the card claims again; only a change reaches ⌘K.
    const held = useActivePullRequestComment.getState().active;
    if (held?.claim === claimToken && held.scope === scope && held.rowsKey === rowsKey) return;
    if (latestRows.current.length > 0) setActive(describe());
  };
  // A held claim follows the card's rows, so ⌘K shows what the menu would.
  useEffect(() => {
    if (!holds) return;
    if (latestRows.current.length === 0) release(claimToken);
    else setActive(describe());
    // `describe` reads only what these name.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [holds, rowsKey, input.commentKey, input.author]);
  // Another workspace user, session or PR: this card's claim ends rather than moving with it.
  const claimedScope = useRef(scope);
  useEffect(() => {
    if (claimedScope.current === scope) return;
    claimedScope.current = scope;
    release(claimToken);
  }, [scope, release, claimToken]);
  useEffect(() => () => release(claimToken), [release, claimToken]);
  const onTimeClick = (event: MouseEvent) => {
    if (!permalink || !hasCommandModifier(event)) return;
    event.preventDefault();
    openLink(conversationId, permalink);
  };
  return { rows, claim, onTimeClick };
}

/**
 * A review comment's actions, shared by every view of a thread: Quote Reply goes to the
 * thread's reply box where replying is allowed; a hidden comment can't be quoted.
 */
export function useReviewCommentActions(
  comment: ReviewComment,
  thread: ReviewThread,
  context: ReviewThreadContext,
  pullRequestUrl: string | null,
  edit?: CommentEditActions | null,
  reactions?: CommentReactionActions | null,
): PullRequestCommentActions {
  const quoteDestination = usePullRequestCommentQuote(
    context.conversationId,
    context.prUrl,
    thread.id,
  );
  return usePullRequestCommentActions({
    conversationId: context.conversationId,
    prUrl: context.prUrl,
    commentKey: comment.id,
    author: comment.author,
    permalink: commentPermalink(comment.url, pullRequestUrl),
    quotableBody: !comment.minimized && comment.body.trim() ? comment.body : null,
    quoteDestination: context.canReply && thread.can_reply ? quoteDestination : null,
    edit,
    showInChanges: context.showInChanges ? () => context.showInChanges?.(thread) : null,
    reactions,
  });
}

/** A conversation comment's actions: Quote Reply goes to the PR's own comment box, if offered. */
export function useConversationCommentActions(
  comment: PullRequestComment,
  context: Pick<ReviewThreadContext, "conversationId" | "prUrl" | "canComment">,
  pullRequestUrl: string | null,
  edit?: CommentEditActions | null,
  reactions?: CommentReactionActions | null,
): PullRequestCommentActions {
  const quoteDestination = usePullRequestCommentQuote(context.conversationId, context.prUrl);
  return usePullRequestCommentActions({
    conversationId: context.conversationId,
    prUrl: context.prUrl,
    commentKey: comment.url ?? `${comment.author ?? ""}@${comment.created_at ?? ""}`,
    author: comment.author,
    permalink: commentPermalink(comment.url, pullRequestUrl),
    quotableBody: comment.body.trim() ? comment.body : null,
    quoteDestination: context.canComment ? quoteDestination : null,
    edit,
    reactions,
  });
}

/** Focus the comment's own ⋯. */
function focusCommentMenu(card: HTMLElement | null): void {
  card?.querySelector<HTMLElement>("[data-comment-menu]")?.focus();
}

/**
 * Where focus goes once this comment is gone: the next visible comment's ⋯ in its own review
 * conversation, else in the same pull request's details (the next, else the previous), else the
 * pull request's own comment box. A conversation's last comment takes the conversation with it.
 */
function afterDeleteTarget(card: HTMLElement | null): HTMLElement | null {
  const own = card?.querySelector<HTMLElement>("[data-comment-menu]");
  if (!card || !own) return null;
  const scope = card.closest<HTMLElement>("[data-pull-request-details]") ?? card.ownerDocument;
  const visible = (element: HTMLElement) => element.checkVisibility?.() !== false;
  const neighborIn = (root: ParentNode, skip: Element | null) => {
    const menus = [...root.querySelectorAll<HTMLElement>("[data-comment-menu]")].filter(
      (menu) => menu === own || (visible(menu) && !(skip && skip.contains(menu))),
    );
    const index = menus.indexOf(own);
    return menus[index + 1] ?? menus[index - 1] ?? null;
  };
  const thread = card.closest("[data-review-thread]");
  const target =
    (thread && neighborIn(thread, null)) ?? neighborIn(scope, thread) ?? conversationBox(scope);
  return target;
}

/** The pull request's own comment box: its folded row, or its first field once open. */
function conversationBox(scope: ParentNode): HTMLElement | null {
  const box = scope.querySelector<HTMLElement>('[data-comment-reply-row="conversation"]');
  if (!box || box.checkVisibility?.() === false) return null;
  return box.querySelector<HTMLElement>("button, textarea, input");
}

/**
 * Where focus goes after the comment is deleted, chosen while the card is still there: the
 * card may be gone by the time GitHub's answer arrives.
 */
function useFocusAfterDelete(cardRef: RefObject<HTMLElement | null>) {
  const neighbor = useRef<HTMLElement | null>(null);
  return {
    remember: () => {
      neighbor.current = afterDeleteTarget(cardRef.current);
    },
    focus: () => {
      const target = neighbor.current;
      neighbor.current = null;
      if (target?.isConnected) target.focus();
    },
  };
}

/** What a comment card needs: its actions, its edit state, and where focus goes after a change. */
export interface PullRequestCommentCard {
  actions: PullRequestCommentActions;
  editing: CommentEditing;
  /** On the card's root, so focus can move to its ⋯ or a neighbor's. */
  cardRef: RefObject<HTMLElement | null>;
  /** Opens the comment on GitHub, where it has a link of its own. */
  openOnGitHub?: () => void;
  /** Its reactions and their picker, which Add Reaction… opens. */
  reactions: CommentReactionsCard;
}

/** A card's reactions: the row's model, its picker, and the commands that open or check them. */
export interface CommentReactionsCard {
  model: CommentReactionsModel | null;
  pickerOpen: boolean;
  setPickerOpen: (open: boolean) => void;
  /** The same rows the ⋯ menu and ⌘K run. */
  commands: CommentReactionActions;
}

/** The card's reactions, and the ⋯ and ⌘K rows that open its picker or check one again. */
function useCommentReactionsCard(
  comment: {
    id?: string;
    database_id?: number | null;
    reactions?: CommentReaction[];
    can_react?: boolean;
  },
  context: { conversationId: string; prUrl: string; threadId?: string },
): CommentReactionsCard {
  const model = useCommentReactions(comment, context);
  const [pickerOpen, setPickerOpen] = useState(false);
  const commands = commentReactionActions({
    openPicker: model?.canReact ? () => setPickerOpen(true) : null,
  });
  return { model, pickerOpen, setPickerOpen, commands };
}

function useCommentCardFocus(editing: CommentEditing, cardRef: RefObject<HTMLElement | null>) {
  // Closing the editor, by Save or Cancel, hands focus back to the comment's ⋯.
  const wasEditing = useRef(editing.entry.editing !== null);
  const nowEditing = editing.entry.editing !== null;
  useEffect(() => {
    if (wasEditing.current && !nowEditing && !editing.entry.tombstone) {
      const card = cardRef.current;
      requestAnimationFrame(() => focusCommentMenu(card));
    }
    wasEditing.current = nowEditing;
  }, [nowEditing, editing.entry.tombstone, cardRef]);
}

/** A conversation comment's card: its actions and its Edit and Delete. */
export function useConversationCommentCard(
  comment: PullRequestComment,
  context: Pick<ReviewThreadContext, "conversationId" | "prUrl" | "canComment">,
  pullRequestUrl: string | null,
): PullRequestCommentCard {
  const cardRef = useRef<HTMLElement | null>(null);
  const afterDelete = useFocusAfterDelete(cardRef);
  const editing = usePullRequestCommentEditing(
    {
      conversationId: context.conversationId,
      prUrl: context.prUrl,
      commentId: comment.id,
      commentNumber: comment.database_id,
      body: comment.body,
      canUpdate: comment.can_update,
      canDelete: comment.can_delete,
      viewerDidAuthor: comment.viewer_did_author,
    },
    { onDeleteConfirmed: afterDelete.remember, onDeleted: afterDelete.focus },
  );
  useCommentCardFocus(editing, cardRef);
  const reactions = useCommentReactionsCard(comment, context);
  const actions = useConversationCommentActions(
    comment,
    context,
    pullRequestUrl,
    editing.actions,
    reactions.commands,
  );
  const permalink = commentPermalink(comment.url, pullRequestUrl);
  return {
    actions,
    editing,
    cardRef,
    openOnGitHub: permalink ? () => openLink(context.conversationId, permalink) : undefined,
    reactions,
  };
}

/** A review comment's card: its actions and its Edit and Delete. */
export function useReviewCommentCard(
  comment: ReviewComment,
  thread: ReviewThread,
  context: ReviewThreadContext,
  pullRequestUrl: string | null,
): PullRequestCommentCard {
  const cardRef = useRef<HTMLElement | null>(null);
  const afterDelete = useFocusAfterDelete(cardRef);
  const editing = usePullRequestCommentEditing(
    {
      conversationId: context.conversationId,
      prUrl: context.prUrl,
      commentId: comment.id,
      commentNumber: comment.database_id,
      threadId: thread.id,
      // A hidden comment's text isn't read, so it can't be edited from here.
      body: comment.body,
      canUpdate: comment.minimized ? false : comment.can_update,
      canDelete: comment.can_delete,
      viewerDidAuthor: comment.viewer_did_author,
      pendingReviewId: comment.pending ? comment.pending_review_id : undefined,
    },
    { onDeleteConfirmed: afterDelete.remember, onDeleted: afterDelete.focus },
  );
  useCommentCardFocus(editing, cardRef);
  // A draft in a pending review isn't published, so nobody can react to it yet.
  const reactions = useCommentReactionsCard(comment.pending ? {} : comment, {
    conversationId: context.conversationId,
    prUrl: context.prUrl,
    threadId: thread.id,
  });
  const actions = useReviewCommentActions(
    comment,
    thread,
    context,
    pullRequestUrl,
    editing.actions,
    reactions.commands,
  );
  const permalink = commentPermalink(comment.url, pullRequestUrl);
  return {
    actions,
    editing,
    cardRef,
    openOnGitHub: permalink ? () => openLink(context.conversationId, permalink) : undefined,
    reactions,
  };
}

/** The ⋯ button and its menu; nothing when the comment offers no actions. */
export function PullRequestCommentMenu({
  actions,
  className,
}: {
  actions: PullRequestCommentActions;
  className?: string;
}) {
  const menu = useMenuCloseHandOff();
  // The editor's and recovery's rows are buttons where they show, and rows in ⌘K.
  const menuRows = actions.rows.filter((row) => !row.definition.paletteOnly);
  if (menuRows.length === 0) return null;
  return (
    <DropdownMenu
      onOpenChange={(open) => {
        if (open) actions.claim();
      }}
    >
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label="Comment actions"
          title="Comment actions"
          data-comment-menu=""
          className={cn("shrink-0 text-muted-foreground", className)}
        >
          <EllipsisIcon aria-hidden className="size-3.5" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" onCloseAutoFocus={menu.onCloseAutoFocus}>
        {menuRows.map((row, index) => {
          const Icon = row.definition.icon;
          const item = (
            <DropdownMenuItem
              key={row.definition.id}
              disabled={!!row.disabledReason}
              variant={row.definition.destructive ? "destructive" : "default"}
              // Quote Reply and Edit put the caret in their box once the menu is closed and can't
              // take it back; Delete…'s dialog returns focus to this ⋯ when it closes.
              onSelect={() =>
                row.definition.movesFocus
                  ? menu.runAfterClose(row.run, {
                      afterFocusReturns: row.definition.id === "delete",
                    })
                  : row.run()
              }
            >
              <Icon aria-hidden />
              {row.disabledReason ? (
                <span className="flex flex-col items-start">
                  {row.definition.menuLabel}
                  <span className="text-xs text-muted-foreground">{row.disabledReason}</span>
                </span>
              ) : (
                row.definition.menuLabel
              )}
            </DropdownMenuItem>
          );
          return row.definition.destructive && index > 0
            ? [<DropdownMenuSeparator key={`${row.definition.id}:separator`} />, item]
            : [item];
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * ⌘K rows for the comment last focused in this panel, while the panel is the one shown. One
 * per panel, so copies of a comment elsewhere on the page never add rows of their own.
 */
export function PullRequestCommentPaletteCommands({
  conversationId,
  prUrl,
  active,
}: {
  conversationId: string;
  prUrl: string | null;
  active: boolean;
}): null {
  const owner = useWorkspaceDraftOwner();
  const scope = prUrl ? commentScope(owner, conversationId, prUrl) : null;
  const comment = useActivePullRequestComment((state) => state.active);
  const claim = comment?.claim;
  const rowsKey = comment?.rowsKey;
  const commands = useMemo<PaletteCommand[]>(() => {
    if (!active || !comment || comment.scope !== scope) return NO_COMMANDS;
    const description = comment.author ? `Comment by ${comment.author}` : undefined;
    return comment.rows().map((row) => {
      const { definition } = row;
      return {
        id: `pull-request-comment:${comment.scope}:${comment.commentKey}:${definition.id}`,
        label: definition.paletteLabel,
        description,
        icon: definition.icon,
        keywords: definition.keywords,
        movesFocus: definition.movesFocus,
        availability: row.disabledReason ? unavailableBecause(row.disabledReason) : undefined,
        run: () => {
          // Only the comment still held runs, with the rows it has now.
          const held = useActivePullRequestComment.getState().active;
          if (!held || held.claim !== claim) return;
          const current = held
            .rows()
            .find((candidate) => candidate.definition.id === definition.id);
          if (current && !current.disabledReason) current.run();
        },
      };
    });
    // The rows are described by `claim`, `rowsKey` and the comment's names.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, scope, claim, rowsKey, comment?.scope, comment?.commentKey, comment?.author]);
  const instance = useId();
  usePaletteCommands(`pull-request-comment:${conversationId}:${instance}`, "Comment", commands);
  return null;
}
