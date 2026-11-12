import type { Comment } from "@/hooks/useComments";
import { FilePathAwareMessageResponse } from "@/components/blocks/ChatMarkdown";

export function LocalReviewCommentPreview({ comment }: { comment: Comment }) {
  const anchor = comment.anchor_metadata!;
  const start = anchor.start_line ?? anchor.line;
  const range = start === anchor.line ? `line ${anchor.line}` : `lines ${start}–${anchor.line}`;
  return (
    <section
      aria-label="Local review comment preview"
      className="my-2 min-w-0 rounded-md border border-border bg-background/50"
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 border-b border-border px-3 py-1.5 text-xs">
        <span className="font-mono">{comment.path}</span>
        <span className="text-muted-foreground">
          {anchor.side === "LEFT" ? "Old" : "New"} {range}
        </span>
      </div>
      <div className="px-3 py-2">
        <FilePathAwareMessageResponse breaks literalHtml mode="static">
          {comment.body}
        </FilePathAwareMessageResponse>
      </div>
      <details className="border-t border-border px-3 py-1.5 text-xs text-muted-foreground">
        <summary className="cursor-pointer hover:text-foreground">Comment reference</summary>
        <dl className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1">
          <dt>Comment</dt>
          <dd className="font-mono">{comment.id}</dd>
          <dt>Pull request</dt>
          <dd>{anchor.pr_url}</dd>
          <dt>Base</dt>
          <dd className="font-mono">{anchor.base_sha}</dd>
          <dt>Head</dt>
          <dd className="font-mono">{anchor.head_sha}</dd>
        </dl>
      </details>
    </section>
  );
}

export function renderLocalReviewComment(comment: Comment) {
  return <LocalReviewCommentPreview comment={comment} />;
}
