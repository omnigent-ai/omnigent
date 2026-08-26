import type { Meta, StoryObj } from "@storybook/react-vite";
import { CopyIcon, RefreshCwIcon } from "lucide-react";
import {
  Message,
  MessageAction,
  MessageActions,
  MessageBranch,
  MessageBranchContent,
  MessageBranchNext,
  MessageBranchPage,
  MessageBranchPrevious,
  MessageBranchSelector,
  MessageContent,
  MessageResponse,
  MessageToolbar,
} from "./message";

const userMarkdown = [
  "Please review **message.tsx** and preserve `min-w-0`.",
  "",
  "Unbroken identifier:",
  "horizontalScrollinghorizontalScrollinghorizontalScrollinghorizontalScrolling",
].join("\n");

const richMarkdown = [
  "## Render contract",
  "",
  "> Markdown should remain readable and secure inside an assistant response.",
  "",
  "1. Preserve headings, lists, links, and tables.",
  "2. Keep prose dollars literal: $/PR, $/session, and $LLM_API_KEY.",
  "",
  "- [x] Completed item",
  "- [ ] Follow-up item",
  "",
  "| Surface | Expected result |",
  "| --- | --- |",
  "| Inline code | `wrap-anywhere` |",
  "| External image | Blocked placeholder |",
  "",
  "[Safe external link](https://example.com/docs)",
  "",
  "![tracking pixel](https://attacker.example/pixel.png)",
].join("\n");

const codeMarkdown = [
  "### TypeScript example",
  "",
  "```ts",
  'type Result = { status: "ready"; answer: number };',
  'const endpoint = "https://example.com/api/results?include=metadata,diagnostics,permissions";',
  'const result: Result = { status: "ready", answer: 42 };',
  "console.log(endpoint, result.answer);",
  "```",
].join("\n");

const mathMarkdown = String.raw`## Quadratic solution

$$x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}$$

The selected response includes a fraction, superscript, and radical.`;

const meta = {
  title: "Components/AI Elements/Message",
  tags: ["visual-snapshot"],
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

export const UserBubbleWrapping: Story = {
  render: () => (
    <div className="w-[640px]">
      <Message from="user">
        <MessageContent>
          <MessageResponse>{userMarkdown}</MessageResponse>
        </MessageContent>
      </Message>
    </div>
  ),
};

export const AssistantRichMarkdownAndActions: Story = {
  render: () => (
    <div className="w-[640px]">
      <Message className="max-w-full" from="assistant">
        <MessageContent className="w-full">
          <MessageResponse>{richMarkdown}</MessageResponse>
        </MessageContent>
        <MessageToolbar>
          <span className="text-xs text-muted-foreground">Complete</span>
          <MessageActions>
            <MessageAction label="Copy response">
              <CopyIcon aria-hidden="true" className="size-3.5" />
            </MessageAction>
            <MessageAction label="Regenerate response">
              <RefreshCwIcon aria-hidden="true" className="size-3.5" />
            </MessageAction>
          </MessageActions>
        </MessageToolbar>
      </Message>
    </div>
  ),
};

export const WrappedHighlightedCode: Story = {
  render: () => (
    <div className="w-[640px]">
      <Message className="max-w-full" from="assistant">
        <MessageContent className="w-full">
          <MessageResponse>{codeMarkdown}</MessageResponse>
        </MessageContent>
      </Message>
    </div>
  ),
};

export const BranchedDisplayMath: Story = {
  render: () => (
    <div className="w-[640px]">
      <MessageBranch defaultBranch={1}>
        <MessageBranchContent>
          <Message key="concise" className="max-w-full" from="assistant">
            <MessageContent className="w-full">
              <MessageResponse>A concise text-only alternative.</MessageResponse>
            </MessageContent>
          </Message>
          <Message key="detailed" className="max-w-full" from="assistant">
            <MessageContent className="w-full">
              <MessageResponse>{mathMarkdown}</MessageResponse>
            </MessageContent>
          </Message>
        </MessageBranchContent>
        <MessageBranchSelector aria-label="Response branches">
          <MessageBranchPrevious />
          <MessageBranchPage />
          <MessageBranchNext />
        </MessageBranchSelector>
      </MessageBranch>
    </div>
  ),
};
