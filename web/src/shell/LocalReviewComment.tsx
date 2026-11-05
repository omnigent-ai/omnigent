import { useState } from "react";
import { PencilIcon, Trash2Icon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { MessageResponse } from "@/components/ai-elements/message";
import { getCurrentAuthorId } from "@/lib/identity";
import { useDeleteComment, useUpdateComment, type Comment } from "@/hooks/useComments";

export function LocalReviewComment({
  comment,
  canEdit,
  outdated = false,
}: {
  comment: Comment;
  canEdit: boolean;
  outdated?: boolean;
}) {
  const update = useUpdateComment(comment.conversation_id);
  const remove = useDeleteComment(comment.conversation_id);
  const [editing, setEditing] = useState(false);
  const [body, setBody] = useState(comment.body);
  const own = canEdit && (!comment.created_by || comment.created_by === getCurrentAuthorId());
  const pending = update.isPending || remove.isPending;
  const anchor = comment.anchor_metadata!;
  return (
    <section
      data-local-review-comment={comment.id}
      tabIndex={-1}
      aria-label="Local review comment"
      className="pull-request-comment-surface my-2 rounded-md border text-ui text-foreground font-sans [white-space:normal]"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="min-w-0 px-3 py-1.5 text-xs text-muted-foreground">
          Local comment · {anchor.side === "LEFT" ? "Old" : "New"} lines{" "}
          {anchor.start_line ?? anchor.line}–{anchor.line}
          {outdated && " · Earlier revision"}
          {comment.status === "addressed" && " · Addressed"}
        </span>
        {own && (
          <div className="flex shrink-0 pr-1 text-muted-foreground">
            <Button
              type="button"
              size="icon-xs"
              variant="ghost"
              aria-label="Edit local comment"
              title="Edit comment"
              disabled={pending || editing}
              onClick={() => {
                setBody(comment.body);
                setEditing(true);
              }}
            >
              <PencilIcon className="size-4" />
            </Button>
            <Button
              type="button"
              size="icon-xs"
              variant="ghost"
              aria-label="Delete local comment"
              title="Delete comment"
              disabled={pending}
              onClick={() => remove.mutate(comment.id)}
            >
              <Trash2Icon className="size-4" />
            </Button>
          </div>
        )}
      </div>
      <div className="px-3 pb-2">
        {editing ? (
          <Textarea
            aria-label="Edit local comment"
            value={body}
            onChange={(event) => setBody(event.target.value)}
          />
        ) : (
          <MessageResponse>{comment.body}</MessageResponse>
        )}
        {own && editing && (
          <div className="mt-2 flex flex-wrap gap-2">
            <Button
              size="sm"
              disabled={pending || !body.trim()}
              onClick={() =>
                update.mutate(
                  { commentId: comment.id, body },
                  { onSuccess: () => setEditing(false) },
                )
              }
            >
              Save
            </Button>
            <Button size="sm" variant="ghost" disabled={pending} onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </div>
        )}
        {(update.error || remove.error) && (
          <p role="alert">{(update.error || remove.error)?.message}</p>
        )}
      </div>
    </section>
  );
}
