import { useEffect, useRef, useState } from "react";
import { BotIcon, WandSparklesIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { Comment } from "@/hooks/useComments";
import {
  useAddCommentReply,
  useCommentReplies,
  useDeleteCommentReply,
  type CommentReply,
} from "@/hooks/useCommentReplies";
import { useOptionalCommentSender } from "@/hooks/CommentSenderContext";
import { getCurrentAuthorId } from "@/lib/identity";
import { hasCommandModifier } from "@/lib/hotkeys";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import { avatarStyle, formatCommentTime } from "./commentPresentation";

interface CommentThreadProps {
  comment: Comment;
  selected: boolean;
  canEdit: boolean;
  canAddress: boolean;
  now: Date;
  onSelect?: () => void;
}

function ownsReply(reply: Pick<CommentReply, "author_kind" | "created_by">) {
  return (
    reply.author_kind === "user" &&
    (reply.created_by == null || reply.created_by === getCurrentAuthorId())
  );
}

function replyName(reply: Pick<CommentReply, "author_kind" | "created_by">) {
  return reply.author_kind === "agent"
    ? (reply.created_by ?? "Agent")
    : ownsReply(reply)
      ? "You"
      : (reply.created_by ?? "You");
}

export function CommentThread(props: CommentThreadProps) {
  return <ThreadDraft key={props.comment.id} {...props} />;
}

function ThreadDraft({ comment, selected, onSelect, ...props }: CommentThreadProps) {
  const [body, setBody] = useState("");
  const count = comment.reply_count ?? 0;
  return (
    <div
      className="space-y-2.5 border-t border-border pt-2.5 text-foreground"
      onClick={(event) => event.stopPropagation()}
    >
      {selected ? (
        <SelectedThread {...props} comment={comment} body={body} setBody={setBody} />
      ) : (
        <button
          type="button"
          aria-expanded={false}
          onClick={onSelect}
          className="block max-w-full truncate text-left text-[11px] text-muted-foreground hover:text-foreground"
        >
          {count ? (
            <>
              {count} {count === 1 ? "reply" : "replies"}
              {comment.last_reply && ` · last ${replyName(comment.last_reply)}`}
            </>
          ) : (
            "Reply…"
          )}
        </button>
      )}
    </div>
  );
}

function SelectedThread({
  comment,
  canEdit,
  canAddress,
  now,
  body,
  setBody,
}: Omit<CommentThreadProps, "selected"> & {
  body: string;
  setBody: (body: string) => void;
}) {
  const repliesQuery = useCommentReplies(
    comment.conversation_id,
    comment.id,
    true,
    comment.updated_at,
  );
  const addReply = useAddCommentReply(comment.conversation_id, comment.id);
  const deleteReply = useDeleteCommentReply(comment.conversation_id, comment.id);
  const sender = useOptionalCommentSender();
  const [expanded, setExpanded] = useState(false);
  const [showEarlier, setShowEarlier] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const composing = useRef(false);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const thread = useRef<HTMLDivElement>(null);
  const firstReply = useRef<HTMLDivElement>(null);
  const submitting = useRef(false);
  const [askingAgent, setAskingAgent] = useState(false);
  useEffect(() => {
    if (showEarlier) firstReply.current?.focus();
  }, [showEarlier]);
  function focusComposer() {
    (textarea.current ?? thread.current)?.focus();
  }
  const replies = repliesQuery.data ?? [];
  const pending = addReply.isPending || askingAgent;
  const canSubmit = !!body.trim() && !pending;

  async function submit(askAgent: boolean) {
    if (!canSubmit || submitting.current) return;
    submitting.current = true;
    setError(null);
    let saved = false;
    try {
      await addReply.mutateAsync(body.trim());
      saved = true;
      setBody("");
      setExpanded(false);
      if (askAgent && canAddress && sender) {
        setAskingAgent(true);
        await sender.mutateAsync({ comment_ids: [comment.id] });
      }
    } catch (failure) {
      const message = failure instanceof Error ? failure.message : "Could not save reply.";
      setError(saved ? `Reply saved. Could not ask agent: ${message}` : message);
      setExpanded(true);
      textarea.current?.focus();
    } finally {
      submitting.current = false;
      setAskingAgent(false);
    }
  }

  return (
    <div
      ref={thread}
      tabIndex={-1}
      role="group"
      aria-label="Comment replies"
      className="space-y-2.5 focus:outline-none"
    >
      {repliesQuery.isPending && <p className="text-xs text-muted-foreground">Loading replies…</p>}
      {repliesQuery.isError && (
        <div role="alert" className="text-xs text-destructive">
          Could not load replies.{" "}
          <button type="button" className="underline" onClick={() => void repliesQuery.refetch()}>
            Retry
          </button>
        </div>
      )}
      {!showEarlier && replies.length > 3 && (
        <button
          type="button"
          className="text-[11px] font-medium text-link hover:underline"
          onClick={() => setShowEarlier(true)}
        >
          Show {replies.length - 3} earlier {replies.length === 4 ? "reply" : "replies"}
        </button>
      )}
      {(showEarlier ? replies : replies.slice(-3)).map((reply, index) => (
        <div
          key={reply.id}
          ref={index === 0 ? firstReply : undefined}
          tabIndex={-1}
          className="group/reply flex gap-2 focus:outline-none"
        >
          <span
            className="mt-0.5 inline-flex size-4 shrink-0 items-center justify-center rounded-full text-[8px] font-semibold uppercase"
            style={reply.author_kind === "agent" ? undefined : avatarStyle(replyName(reply))}
          >
            {reply.author_kind === "agent" ? (
              <BotIcon className="size-4 text-primary" />
            ) : (
              replyName(reply)[0]?.toUpperCase()
            )}
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-baseline gap-1.5">
              <span className="truncate text-xs font-medium">{replyName(reply)}</span>
              {reply.author_kind === "agent" && (
                <span className="rounded border border-primary/40 px-1 text-[9px] text-primary">
                  Agent
                </span>
              )}
              <span className="ml-auto shrink-0 text-[10px] text-muted-foreground">
                {formatCommentTime(reply.created_at / 1_000_000, now)}
              </span>
              {canEdit && ownsReply(reply) && (
                <button
                  type="button"
                  aria-label="Delete reply"
                  disabled={deleteReply.isPending}
                  className="shrink-0 text-[11px] text-muted-foreground opacity-0 hover:text-destructive group-hover/reply:opacity-100 group-focus-within/reply:opacity-100 [@media(hover:none)]:opacity-100"
                  onClick={() => {
                    setError(null);
                    deleteReply.mutate(reply.id, {
                      onSuccess: focusComposer,
                      onError: (failure) => {
                        setError(failure.message);
                        focusComposer();
                      },
                    });
                  }}
                >
                  Delete
                </button>
              )}
            </div>
            <p className="mt-0.5 whitespace-pre-wrap break-words text-sm leading-relaxed">
              {reply.body}
            </p>
          </div>
        </div>
      ))}
      {canEdit && (
        <form
          className="space-y-1.5"
          onSubmit={(event) => {
            event.preventDefault();
            void submit(false);
          }}
        >
          <textarea
            ref={textarea}
            aria-label="Reply to comment"
            placeholder="Reply…"
            rows={expanded ? 3 : 1}
            readOnly={pending}
            aria-busy={pending}
            value={body}
            className="w-full resize-none rounded-md border border-border bg-background px-2 py-1.5 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
            onFocus={() => setExpanded(true)}
            onChange={(event) => {
              setBody(event.target.value);
              setError(null);
            }}
            onCompositionStart={() => {
              composing.current = true;
            }}
            onCompositionEnd={() => {
              composing.current = false;
            }}
            onKeyDown={(event) => {
              if (isImeCompositionKeyEvent(event, composing.current)) return;
              if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                setExpanded(false);
                textarea.current?.blur();
              }
              if (event.key === "Enter" && hasCommandModifier(event) && !event.repeat) {
                event.preventDefault();
                void submit(false);
              }
            }}
          />
          {expanded && (
            <div className="flex flex-wrap justify-end gap-1.5">
              <Button
                type="button"
                size="xs"
                variant="ghost"
                disabled={pending}
                onClick={() => {
                  setExpanded(false);
                  textarea.current?.blur();
                }}
              >
                Cancel
              </Button>
              {canAddress && sender && (
                <Button
                  type="button"
                  size="xs"
                  variant="outline"
                  disabled={!canSubmit}
                  onClick={() => void submit(true)}
                >
                  <WandSparklesIcon className="size-3" />
                  Ask agent
                </Button>
              )}
              <Button type="submit" size="xs" disabled={!canSubmit}>
                Reply
              </Button>
            </div>
          )}
        </form>
      )}
      {error && (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
