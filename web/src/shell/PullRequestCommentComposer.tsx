import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { MessageResponse } from "@/components/ai-elements/message";
import {
  MAX_PULL_REQUEST_COMMENT_CHARACTERS,
  pullRequestCommentTooLong,
  usePullRequestCommentComposer,
  type PullRequestCommentDraft,
} from "@/hooks/usePullRequests";
import { PULL_REQUEST_MARKDOWN_COMPONENTS } from "./PullRequestDetails";

export function PullRequestCommentComposer({
  conversationId,
  prUrl,
  account,
}: {
  conversationId: string;
  prUrl: string;
  account: string | null;
}) {
  const { draft, setBody, post } = usePullRequestCommentComposer(conversationId, prUrl);
  return <PullRequestCommentForm draft={draft} setBody={setBody} post={post} account={account} />;
}

export function PullRequestCommentForm({
  draft,
  setBody,
  post,
  account,
  inline = false,
  newThread = false,
  disabled = false,
}: {
  draft: PullRequestCommentDraft;
  setBody: (body: string) => void;
  post: () => void;
  account: string | null;
  inline?: boolean;
  newThread?: boolean;
  disabled?: boolean;
}) {
  const [tab, setTab] = useState("write");
  const formRef = useRef<HTMLFormElement>(null);
  const compactReply = inline && !newThread;
  const [replyExpanded, setReplyExpanded] = useState(
    !!draft.body || !!draft.error || draft.pending,
  );
  const wasPending = useRef(draft.pending);
  useEffect(() => {
    if (compactReply && wasPending.current && !draft.pending && !draft.error && !draft.body) {
      setReplyExpanded(false);
      setTab("write");
    }
    wasPending.current = draft.pending;
  }, [compactReply, draft.pending, draft.error, draft.body]);
  const collapsed = compactReply && !replyExpanded && !draft.pending;
  const tooLong = pullRequestCommentTooLong(draft.body);
  return (
    <form
      ref={formRef}
      tabIndex={compactReply ? -1 : undefined}
      className={
        collapsed
          ? "pull-request-comment-surface min-w-0 border-t p-2"
          : inline
            ? "pull-request-comment-surface flex max-h-96 min-w-0 flex-col gap-2 border-t p-3"
            : "pull-request-comment-surface flex max-h-[60%] shrink-0 flex-col gap-2 border-t p-3"
      }
      onSubmit={(event) => {
        event.preventDefault();
        if (!disabled && !collapsed) post();
      }}
    >
      {collapsed ? (
        <Input
          aria-label="Reply to review conversation"
          aria-expanded={false}
          placeholder="Write a reply…"
          value={draft.body}
          onChange={(event) => setBody(event.target.value)}
          onFocus={() => setReplyExpanded(true)}
          className="bg-transparent focus-visible:border-input focus-visible:outline-none focus-visible:ring-0 dark:bg-transparent"
        />
      ) : (
        <>
          <Tabs value={tab} onValueChange={setTab} className="min-h-0">
            <TabsList className="shrink-0">
              <TabsTrigger value="write">Write</TabsTrigger>
              <TabsTrigger value="preview">Preview</TabsTrigger>
            </TabsList>
            <TabsContent value="write" className="min-h-0 overflow-y-auto">
              <Textarea
                aria-label={
                  newThread
                    ? "New review comment"
                    : inline
                      ? "Reply to review conversation"
                      : "New PR comment"
                }
                className="max-h-40 bg-transparent focus-visible:border-input focus-visible:outline-none focus-visible:ring-0 dark:bg-transparent"
                placeholder={inline && !newThread ? "Write a reply…" : "Leave a comment..."}
                autoFocus={newThread || compactReply}
                value={draft.body}
                disabled={draft.pending}
                onChange={(event) => setBody(event.target.value)}
                rows={inline ? 2 : 4}
              />
            </TabsContent>
            <TabsContent value="preview" className="min-h-0 overflow-y-auto">
              {draft.body.trim() ? (
                <MessageResponse
                  allowGitHubAttachmentImages
                  components={PULL_REQUEST_MARKDOWN_COMPONENTS}
                >
                  {draft.body}
                </MessageResponse>
              ) : (
                <p className="text-muted-foreground">Nothing to preview.</p>
              )}
            </TabsContent>
          </Tabs>
          {tooLong && (
            <p role="alert">
              A comment can be at most {MAX_PULL_REQUEST_COMMENT_CHARACTERS} characters.
            </p>
          )}
          {draft.error && (
            <p role="alert" className="text-destructive">
              {draft.error}
            </p>
          )}
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <Button
              type="submit"
              size="sm"
              className="shrink-0"
              disabled={disabled || draft.pending || !draft.body.trim() || tooLong}
            >
              {draft.pending ? "Posting…" : inline && !newThread ? "Reply" : "Post comment"}
            </Button>
            {compactReply && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={draft.pending}
                onClick={() => {
                  setReplyExpanded(false);
                  setTab("write");
                  formRef.current?.focus();
                }}
              >
                Cancel
              </Button>
            )}
            <p
              className="min-w-0 truncate text-ui text-muted-foreground"
              title={account ?? undefined}
            >
              {account ? `Comment as ${account}` : "Comment using the host’s GitHub account"}
            </p>
          </div>
        </>
      )}
    </form>
  );
}
