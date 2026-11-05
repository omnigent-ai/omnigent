import { MessageSquareTextIcon, XIcon } from "lucide-react";
import { useDeleteComment, type Comment } from "@/hooks/useComments";
import { toast } from "sonner";
import { useOpenGithubTab } from "@/shell/FileViewerContext";
import { requestLocalReviewNavigation } from "@/lib/localReviewContext";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { ComposerChipRow } from "./ChatComposer";

export function ComposerLocalReviewChips({ comments }: { comments: Comment[] }) {
  if (comments.length === 0) return null;
  return (
    <ComposerChipRow className="gap-1.5">
      {comments.map((comment) => (
        <LocalReviewChip key={comment.id} comment={comment} />
      ))}
    </ComposerChipRow>
  );
}

function LocalReviewChip({ comment }: { comment: Comment }) {
  const openPullRequests = useOpenGithubTab();
  const remove = useDeleteComment(comment.conversation_id);
  const anchor = comment.anchor_metadata!;
  const line = anchor.start_line ? `${anchor.start_line}–${anchor.line}` : `${anchor.line}`;
  const label = `${comment.path}:${line} (${anchor.side === "LEFT" ? "old" : "new"})`;
  return (
    <HoverCard key={comment.id} openDelay={250} closeDelay={100}>
      <span className="flex max-w-[260px] items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-sm text-muted-foreground">
        <HoverCardTrigger asChild>
          <button
            type="button"
            className="flex min-w-0 items-center gap-1 hover:text-foreground"
            aria-label={`Open local review comment ${label}`}
            onClick={() => {
              requestLocalReviewNavigation(comment);
              openPullRequests?.();
            }}
          >
            <MessageSquareTextIcon className="size-3 shrink-0" />
            <span className="truncate">{label}</span>
          </button>
        </HoverCardTrigger>
        <button
          type="button"
          onClick={() =>
            remove.mutate(comment.id, {
              onError: (error) => toast.error(`Couldn't delete comment: ${error.message}`),
            })
          }
          disabled={remove.isPending}
          className="ml-0.5 shrink-0 rounded-full hover:text-foreground disabled:opacity-50"
          aria-label={`Delete local review comment ${label}`}
          title="Delete comment"
        >
          <XIcon className="size-3" />
        </button>
      </span>
      <HoverCardContent
        className="max-h-64 w-80 max-w-[calc(100vw-2rem)] overflow-y-auto break-words"
        align="start"
      >
        <p className="mb-1 break-all text-xs text-muted-foreground">
          {comment.path} · {anchor.side === "LEFT" ? "Old" : "New"} line {line}
        </p>
        <p className="whitespace-pre-wrap text-sm">{comment.body}</p>
      </HoverCardContent>
    </HoverCard>
  );
}
