import { useState } from "react";
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
  const anchor = comment.review_anchor!;
  return (
    <section
      aria-label="Local review comment"
      className="pull-request-comment-surface my-2 rounded-md border p-3 text-ui text-foreground font-sans [white-space:normal]"
    >
      <p className="mb-2 text-xs text-muted-foreground">
        Local comment · {anchor.side === "LEFT" ? "Old" : "New"} lines{" "}
        {anchor.start_line ?? anchor.line}–{anchor.line}
        {outdated && " · Earlier revision"}
        {comment.status === "addressed" && " · Addressed"}
      </p>
      {editing ? (
        <Textarea
          aria-label="Edit local comment"
          value={body}
          onChange={(event) => setBody(event.target.value)}
        />
      ) : (
        <MessageResponse>{comment.body}</MessageResponse>
      )}
      {own && (
        <div className="mt-2 flex gap-2">
          {editing ? (
            <>
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
              <Button
                size="sm"
                variant="ghost"
                disabled={pending}
                onClick={() => setEditing(false)}
              >
                Cancel
              </Button>
            </>
          ) : (
            <>
              <Button
                size="sm"
                variant="ghost"
                disabled={pending}
                onClick={() => {
                  setBody(comment.body);
                  setEditing(true);
                }}
              >
                Edit
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={pending}
                onClick={() =>
                  update.mutate({
                    commentId: comment.id,
                    status: comment.status === "draft" ? "addressed" : "draft",
                  })
                }
              >
                {comment.status === "draft" ? "Mark addressed" : "Reopen"}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={pending}
                onClick={() => remove.mutate(comment.id)}
              >
                Delete
              </Button>
            </>
          )}
        </div>
      )}
      {(update.error || remove.error) && (
        <p role="alert">{(update.error || remove.error)?.message}</p>
      )}
    </section>
  );
}
