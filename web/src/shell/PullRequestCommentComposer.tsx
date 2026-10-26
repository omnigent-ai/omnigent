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
      className="space-y-2 border-t p-3"
      onSubmit={(event) => {
        event.preventDefault();
        post();
      }}
    >
      <p className="text-ui text-muted-foreground">
        {account ? `Comment as ${account}` : "Comment using the host’s GitHub account"}
      </p>
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="write">Write</TabsTrigger>
          <TabsTrigger value="preview">Preview</TabsTrigger>
        </TabsList>
        <TabsContent value="write">
          <Textarea
            aria-label="New PR comment"
            placeholder="Leave a comment… Markdown is supported."
            value={draft.body}
            disabled={draft.pending}
            onChange={(event) => setBody(event.target.value)}
            rows={4}
          />
        </TabsContent>
        <TabsContent value="preview">
          {draft.body.trim() ? (
            <MessageResponse components={PULL_REQUEST_MARKDOWN_COMPONENTS}>
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
      <Button type="submit" size="sm" disabled={draft.pending || !draft.body.trim() || tooLong}>
        {draft.pending ? "Posting…" : "Post comment"}
      </Button>
    </form>
  );
}
