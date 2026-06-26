import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { CheckIcon, Link2Icon, WandSparklesIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { useResizableCommentsPanel } from "@/hooks/useResizableCommentsPanel";
import { getCurrentAuthorId } from "@/lib/identity";
import { cn } from "@/lib/utils";
import type { Comment } from "@/hooks/useComments";
import type { ActiveSelection } from "./codeViewerHelpers";

function avatarStyle(name: string): { backgroundColor: string; color: string } {
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
  return {
    backgroundColor: `hsl(${hash % 360} var(--avatar-saturation) var(--avatar-lightness))`,
    color: "var(--avatar-fg)",
  };
}

function formatCommentTime(createdAt: number): string {
  const date = new Date(createdAt * 1000);
  const now = new Date();
  const time = date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  if (date.toDateString() === now.toDateString()) return `${time} Today`;
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return `${time} Yesterday`;
  return `${date.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${time}`;
}

// ---------------------------------------------------------------------------
// CommentsPanel — right panel for adding and viewing comments. Resizable on
// desktop via a left-edge drag handle (see useResizableCommentsPanel); the
// chosen width persists across panel remounts within a session.
// ---------------------------------------------------------------------------

export type { ActiveSelection };

export interface CommentsPanelProps {
  comments: Comment[];
  addressedComments: Comment[];
  activeSelection: ActiveSelection | null;
  onAddComment: (body: string) => void;
  onAddressAll: () => void;
  onEditComment: (id: string, body: string) => void;
  onDeleteComment: (id: string) => void;
  onClickComment: (comment: Comment) => void;
  /** When false, "Address All" is disabled (no agent registered). */
  canAddress: boolean;
  addressPending: boolean;
  /**
   * When false, the add-comment form and all edit/delete buttons are hidden
   * (read-only access). When true, the add-comment form is shown, but the
   * per-comment edit/delete buttons appear only on the current user's own
   * comments — see `canModify`. Defaults to true (single-user mode or
   * owner/editor access).
   */
  canEdit?: boolean;
  /**
   * Called when the user clicks the "Copy link" icon on a comment.
   * The caller is responsible for building the URL and writing to clipboard.
   */
  onCopyCommentLink?: (commentId: string) => void;
  /**
   * Shared mutable ref to the current textarea body. CommentsPanel writes to
   * it on every keystroke so MarkdownCommentPlugin can check whether there's
   * a draft before deciding to clear the pending mark on click-away.
   */
  pendingBodyRef?: RefObject<string>;
}

type Tab = "open" | "addressed";
const TABS: Tab[] = ["open", "addressed"];

export function CommentsPanel({
  comments,
  addressedComments,
  activeSelection,
  onAddComment,
  onAddressAll,
  onEditComment,
  onDeleteComment,
  onClickComment,
  canAddress,
  addressPending,
  canEdit = true,
  pendingBodyRef,
  onCopyCommentLink,
}: CommentsPanelProps) {
  const [body, setBody] = useState("");
  const [tab, setTab] = useState<Tab>("open");
  const addCommentTextareaRef = useRef<HTMLTextAreaElement>(null);
  const { width, containerRef, isDesktop, handleProps } = useResizableCommentsPanel();

  // Editing or deleting a comment is author-only (the backend enforces this
  // too; this just hides the affordances). A comment with no recorded author
  // (legacy comments, or single-user/local mode where currentAuthorId is null)
  // stays editable by any editor, matching the server's `created_by is None`
  // fallback.
  const currentAuthorId = getCurrentAuthorId();
  const canModify = (c: Comment): boolean =>
    canEdit && (c.created_by == null || c.created_by === currentAuthorId);

  useEffect(() => {
    setBody("");
    if (pendingBodyRef) pendingBodyRef.current = "";
  }, [activeSelection?.start_index, activeSelection?.end_index, pendingBodyRef]);

  // Auto-focus the textarea when a new pending selection appears (no existing
  // comment at that range) so the user can start typing immediately.
  useEffect(() => {
    if (!activeSelection) return;
    const isExisting = comments.some(
      (c) =>
        c.start_index === activeSelection.start_index && c.end_index === activeSelection.end_index,
    );
    if (!isExisting) {
      // rAF ensures the textarea has been rendered before we try to focus it.
      const id = requestAnimationFrame(() => addCommentTextareaRef.current?.focus());
      return () => cancelAnimationFrame(id);
    }
  }, [activeSelection, comments]);

  return (
    <div
      ref={containerRef}
      style={isDesktop && width != null ? { width } : undefined}
      className="relative flex shrink-0 flex-col overflow-hidden border-border w-full h-64 border-t md:h-auto md:border-t-0 md:border-l"
    >
      {/* Resize handle — desktop only (mobile stacks the panel full-width below) */}
      {isDesktop && (
        <div
          {...handleProps}
          className="absolute inset-y-0 left-0 z-10 w-1 cursor-col-resize hover:bg-primary/30 active:bg-primary/50 transition-colors"
        />
      )}
      {/* Header — fixed height so layout doesn't shift when button is hidden */}
      <div className="flex h-11 shrink-0 items-center justify-between px-3 border-b border-border">
        <span className="text-xs font-semibold">Comments</span>
        {tab === "open" && (
          <Button
            type="button"
            variant="outline"
            size="xs"
            className="rounded-full px-3 gap-1.5"
            disabled={!canAddress || comments.length === 0 || addressPending}
            onClick={onAddressAll}
          >
            <WandSparklesIcon className="size-3.5" />
            Address All
          </Button>
        )}
      </div>

      {/* Tabs */}
      <div className="flex shrink-0 border-b border-border">
        {TABS.map((t) => {
          const count = t === "open" ? comments.length : addressedComments.length;
          return (
            <button
              key={t}
              type="button"
              className={cn(
                "flex-1 py-1.5 text-[11px] font-medium capitalize transition-colors cursor-pointer",
                tab === t
                  ? "border-b-2 border-primary text-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )}
              onClick={() => setTab(t)}
            >
              {t === "open" ? "Open" : "Addressed"}
              {count > 0 && (
                <span className="ml-1 rounded-full bg-muted px-1.5 py-0.5 text-[10px] tabular-nums">
                  {count}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {!canEdit && (
        <div className="shrink-0 border-b border-border px-3 py-2 text-xs text-muted-foreground">
          You have read-only access to this session.
        </div>
      )}

      <div className="flex-1 overflow-y-auto">
        {/* Input section — shown when text is selected with no existing comment at same range and user can edit */}
        {tab === "open" &&
          activeSelection != null &&
          !comments.some(
            (c) =>
              c.start_index === activeSelection.start_index &&
              c.end_index === activeSelection.end_index,
          ) &&
          (canEdit ? (
            <div className="space-y-2 border-b border-border px-3 py-2">
              {activeSelection.anchor_content && (
                <div className="truncate rounded bg-muted/40 px-2 py-1 font-mono text-[10px] text-muted-foreground">
                  <span className="text-foreground/60">Selection: </span>
                  {activeSelection.anchor_content.trim().split("\n")[0]}
                </div>
              )}
              <textarea
                ref={addCommentTextareaRef}
                className="w-full resize-none rounded border border-border bg-background px-2 py-1.5 text-xs placeholder:text-muted-foreground"
                rows={3}
                placeholder="Add a comment…"
                value={body}
                onChange={(e) => {
                  setBody(e.target.value);
                  if (pendingBodyRef) pendingBodyRef.current = e.target.value;
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey && body.trim()) {
                    e.preventDefault();
                    onAddComment(body.trim());
                    setBody("");
                    if (pendingBodyRef) pendingBodyRef.current = "";
                  }
                }}
              />
              <Button
                type="button"
                size="xs"
                className="w-full"
                disabled={!body.trim()}
                onClick={() => {
                  onAddComment(body.trim());
                  setBody("");
                  if (pendingBodyRef) pendingBodyRef.current = "";
                }}
              >
                Add Comment
              </Button>
            </div>
          ) : null)}

        {/* Comment list */}
        {tab === "open" ? (
          comments.length === 0 ? (
            <div className="flex items-center justify-center p-8 text-xs text-muted-foreground">
              No open comments.
            </div>
          ) : (
            <div className="space-y-2 p-3">
              {comments.map((c) => (
                <CommentCard
                  key={c.id}
                  comment={c}
                  isSelected={
                    activeSelection?.start_index === c.start_index &&
                    activeSelection?.end_index === c.end_index
                  }
                  onClick={() => onClickComment(c)}
                  onDelete={canModify(c) ? () => onDeleteComment(c.id) : undefined}
                  onEdit={canModify(c) ? (newBody) => onEditComment(c.id, newBody) : undefined}
                  onCopyLink={onCopyCommentLink ? () => onCopyCommentLink(c.id) : undefined}
                />
              ))}
            </div>
          )
        ) : addressedComments.length === 0 ? (
          <div className="flex items-center justify-center p-8 text-xs text-muted-foreground">
            No addressed comments.
          </div>
        ) : (
          <div className="space-y-2 p-3">
            {addressedComments.map((c) => (
              <CommentCard
                key={c.id}
                comment={c}
                onDelete={canModify(c) ? () => onDeleteComment(c.id) : undefined}
                onCopyLink={onCopyCommentLink ? () => onCopyCommentLink(c.id) : undefined}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// CommentCard — single comment in a bordered card with inline edit support
// ---------------------------------------------------------------------------

interface CommentCardProps {
  comment: Comment;
  isSelected?: boolean;
  onClick?: () => void;
  onEdit?: (body: string) => void;
  onDelete?: () => void;
  onCopyLink?: () => void;
}

function CommentCard({
  comment: c,
  isSelected,
  onClick,
  onEdit,
  onDelete,
  onCopyLink,
}: CommentCardProps) {
  const [editing, setEditing] = useState(false);
  const [editBody, setEditBody] = useState(c.body);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [linkCopied, setLinkCopied] = useState(false);
  const linkCopiedTimerRef = useRef<number>(0);

  // Google-Docs-style "Show more": collapse a long body to a few lines and
  // reveal a toggle only when it actually overflows. `clamped` is measured
  // while collapsed and re-checked on width changes (the panel is resizable).
  const bodyRef = useRef<HTMLParagraphElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [clamped, setClamped] = useState(false);

  useEffect(
    () => () => {
      window.clearTimeout(linkCopiedTimerRef.current);
    },
    [],
  );

  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (!el || editing || expanded) return;
    const measure = () => setClamped(el.scrollHeight > el.clientHeight + 1);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [c.body, editing, expanded]);

  // Collapse when switching comments so a long body never opens pre-expanded.
  useEffect(() => {
    setExpanded(false);
  }, [c.id]);

  useEffect(() => {
    if (!editing) setEditBody(c.body);
  }, [c.id, c.body, editing]);

  const statusLabel = c.status === "addressed" ? "Addressed" : null;

  function startEdit() {
    setEditBody(c.body);
    setEditing(true);
    setTimeout(() => textareaRef.current?.focus(), 0);
  }

  function saveEdit() {
    if (editBody.trim()) onEdit?.(editBody.trim());
    setEditing(false);
  }

  return (
    <div
      className={cn(
        "rounded-lg border p-3 space-y-2 transition-colors",
        isSelected
          ? "border-primary bg-primary/10 ring-1 ring-primary/30 cursor-default"
          : "border-border bg-muted/20 cursor-pointer hover:border-foreground/20",
      )}
      onClick={() => {
        if (!editing) onClick?.();
      }}
    >
      {/* Anchor */}
      {c.anchor_content && (
        <p className="truncate font-mono text-[11px] text-muted-foreground">
          {c.anchor_content.trim()}
        </p>
      )}

      {/* Comment body — click to edit */}
      {editing ? (
        <div className="space-y-1.5">
          <textarea
            ref={textareaRef}
            className="w-full resize-none rounded border border-border bg-background px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
            rows={3}
            value={editBody}
            onChange={(e) => setEditBody(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) saveEdit();
              if (e.key === "Escape") setEditing(false);
            }}
          />
          <div className="flex gap-1.5">
            <Button type="button" size="xs" disabled={!editBody.trim()} onClick={saveEdit}>
              Save
            </Button>
            <Button type="button" size="xs" variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <div className="space-y-1">
          <p
            ref={bodyRef}
            className={cn(
              "text-xs leading-relaxed text-foreground break-words whitespace-pre-wrap",
              !expanded && "line-clamp-4",
            )}
          >
            {c.body}
          </p>
          {(clamped || expanded) && (
            <button
              type="button"
              aria-expanded={expanded}
              className="cursor-pointer text-[10px] font-medium text-blue-600 hover:underline dark:text-blue-400"
              onClick={(e) => {
                e.stopPropagation();
                setExpanded((v) => !v);
              }}
            >
              {expanded ? "Show less" : "Show more"}
            </button>
          )}
        </div>
      )}

      {/* Footer: user/time on left, actions on right */}
      {!editing && (
        <div className="flex items-end justify-between gap-2">
          <div className="flex min-w-0 flex-col gap-0.5">
            <div className="flex min-w-0 items-center gap-1.5">
              <span
                className="inline-flex size-4 shrink-0 items-center justify-center rounded-full text-[8px] font-semibold uppercase"
                style={avatarStyle(c.created_by ?? "You")}
              >
                {(c.created_by ?? "Y")[0].toUpperCase()}
              </span>
              <TooltipProvider>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span className="truncate text-[11px] text-muted-foreground">
                      {c.created_by ?? "You"}
                    </span>
                  </TooltipTrigger>
                  <TooltipContent>{c.created_by ?? "You"}</TooltipContent>
                </Tooltip>
              </TooltipProvider>
            </div>
            <span className="text-[10px] text-muted-foreground/70">
              {formatCommentTime(c.created_at)}
            </span>
            {statusLabel && (
              <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] w-fit">
                {statusLabel}
              </span>
            )}
          </div>
          {(onEdit || onDelete || onCopyLink) && (
            <div className="flex shrink-0 items-center gap-2 mr-0.5">
              {onEdit && (
                <button
                  type="button"
                  className="cursor-pointer text-[11px] text-muted-foreground transition-colors hover:text-foreground"
                  onClick={(e) => {
                    e.stopPropagation();
                    startEdit();
                  }}
                >
                  Edit
                </button>
              )}
              {onDelete && (
                <button
                  type="button"
                  className="cursor-pointer text-[11px] text-muted-foreground transition-colors hover:text-destructive"
                  onClick={(e) => {
                    e.stopPropagation();
                    onDelete();
                  }}
                >
                  Delete
                </button>
              )}
              {onCopyLink && (
                <button
                  type="button"
                  aria-label="Copy link to comment"
                  className="cursor-pointer text-[11px] text-muted-foreground transition-colors hover:text-foreground"
                  onClick={(e) => {
                    e.stopPropagation();
                    onCopyLink();
                    setLinkCopied(true);
                    window.clearTimeout(linkCopiedTimerRef.current);
                    linkCopiedTimerRef.current = window.setTimeout(
                      () => setLinkCopied(false),
                      2000,
                    );
                  }}
                >
                  {linkCopied ? <CheckIcon className="size-3" /> : <Link2Icon className="size-3" />}
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
