import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { MessageResponse } from "@/components/ai-elements/message";
import {
  MAX_PULL_REQUEST_COMMENT_CHARACTERS,
  pullRequestCommentTooLong,
  usePullRequestCommentComposer,
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
  const [tab, setTab] = useState("write");
  const tooLong = pullRequestCommentTooLong(draft.body);
  return (
    <form
      className="flex max-h-[60%] shrink-0 flex-col gap-2 border-t bg-background p-3"
      onSubmit={(event) => {
        event.preventDefault();
        post();
      }}
    >
      <Tabs value={tab} onValueChange={setTab} className="min-h-0">
        <TabsList className="shrink-0">
          <TabsTrigger value="write">Write</TabsTrigger>
          <TabsTrigger value="preview">Preview</TabsTrigger>
        </TabsList>
        <TabsContent value="write" className="min-h-0 overflow-y-auto">
          <Textarea
            aria-label="New PR comment"
            className="max-h-40 bg-transparent focus-visible:ring-0 dark:bg-transparent"
            placeholder="Leave a comment..."
            value={draft.body}
            disabled={draft.pending}
            onChange={(event) => setBody(event.target.value)}
            rows={4}
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
      <div className="flex shrink-0 items-center gap-2">
        <Button
          type="submit"
          size="sm"
          className="shrink-0"
          disabled={draft.pending || !draft.body.trim() || tooLong}
        >
          {draft.pending ? "Posting…" : "Post comment"}
        </Button>
        <p className="min-w-0 truncate text-ui text-muted-foreground" title={account ?? undefined}>
          {account ? `Comment as ${account}` : "Comment using the host’s GitHub account"}
        </p>
      </div>
    </form>
  );
}
