import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { CommentSenderProvider } from "@/hooks/CommentSenderContext";
import type { Meta, StoryObj } from "@storybook/react-vite";
import { userEvent, within } from "storybook/test";
import type { Comment } from "@/hooks/useComments";
import { CommentsPanel } from "./CommentsPanel";

const now = new Date("2024-06-15T12:00:00Z");

function comment(overrides: Partial<Comment> & { id: string }): Comment {
  const createdAt = 1_718_440_200;
  return {
    conversation_id: "conversation-story",
    path: "src/app.py",
    start_index: 0,
    end_index: 10,
    body: "Consider extracting this branch into a helper.",
    status: "draft",
    created_at: createdAt,
    updated_at: createdAt * 1_000_000,
    anchor_content: "def compute_totals(rows):",
    created_by: null,
    ...overrides,
  };
}

const openComments = [
  comment({ id: "comment-1", start_index: 120, end_index: 158 }),
  comment({
    id: "comment-2",
    start_index: 200,
    end_index: 240,
    created_by: "reviewer@example.com",
    body: "This branch handles several independent concerns.\nPlease separate validation, normalization, persistence, and notification so each path can be reviewed and tested independently.\nThe current ordering also makes the error handling difficult to follow.",
    anchor_content: "result = validate_and_persist(payload)",
  }),
  comment({
    id: "comment-3",
    start_index: 280,
    end_index: 310,
    created_at: 1_717_232_400,
    body: "Add a focused regression test for the empty input case.",
    anchor_content: "if not rows:",
  }),
];

const addressedComments = [
  comment({
    id: "addressed-1",
    status: "addressed",
    start_index: 20,
    end_index: 40,
    body: "This naming issue was resolved.",
  }),
  comment({
    id: "addressed-2",
    status: "addressed",
    start_index: 50,
    end_index: 70,
    created_by: "reviewer@example.com",
    body: "The missing type annotation was added.",
  }),
];

const meta = {
  title: "Components/Shell/CommentsPanel",
  component: CommentsPanel,
  tags: ["visual-snapshot"],
  args: {
    comments: openComments,
    addressedComments,
    activeSelection: null,
    onAddComment: () => undefined,
    onAddressAll: () => undefined,
    onEditComment: () => undefined,
    onDeleteComment: () => undefined,
    onClickComment: () => undefined,
    onCopyCommentLink: () => undefined,
    canAddress: true,
    addressPending: false,
    canEdit: true,
    now,
  },
  decorators: [
    (Story, context) => (
      <div
        className={
          context.parameters.threadDemo
            ? "flex h-[640px] w-[360px] justify-end overflow-hidden rounded-lg border bg-muted/20 text-foreground [&>div]:!w-[360px]"
            : "flex h-[600px] w-[900px] justify-end overflow-hidden rounded-lg border bg-muted/20 [&>div]:!w-[360px]"
        }
      >
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof CommentsPanel>;

export default meta;
type Story = StoryObj<typeof meta>;

export const OpenPopulated: Story = {
  args: {
    activeSelection: {
      start_index: 120,
      end_index: 158,
      anchor_content: "def compute_totals(rows):",
    },
  },
};

export const ComposerWithEmptyList: Story = {
  args: {
    comments: [],
    addressedComments: [],
    activeSelection: {
      start_index: 42,
      end_index: 88,
      anchor_content: "return sum(row.amount for row in rows)",
    },
  },
  play: async ({ canvasElement }) => {
    await userEvent.type(
      within(canvasElement).getByPlaceholderText("Add a comment…"),
      "This should handle empty rows.",
    );
  },
};

export const InlineEditing: Story = {
  args: {
    comments: [
      comment({
        id: "editable-comment",
        body: "Rename this variable.",
        anchor_content: "total = 0",
      }),
    ],
    addressedComments: [],
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await userEvent.click(canvas.getByRole("button", { name: "Edit" }));
    const textarea = canvas.getByRole("textbox");
    await userEvent.clear(textarea);
    await userEvent.type(textarea, "Rename this variable to running_total.");
  },
};

export const ReadOnlyAddressedTab: Story = {
  args: {
    comments: [openComments[0]!],
    addressedComments,
    canEdit: false,
    canAddress: false,
    activeSelection: {
      start_index: 5,
      end_index: 20,
      anchor_content: "import os",
    },
  },
  play: async ({ canvasElement }) => {
    await userEvent.click(within(canvasElement).getByRole("button", { name: /sent/i }));
  },
};

const threadClient = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity } } });
threadClient.setQueryData(
  ["comments", "conversation-story", "replies", "thread-comment", 1_718_440_200 * 1_000_000],
  [
    {
      id: "reply-1",
      author_kind: "agent",
      created_by: null,
      body: "The offsets keep comments in file order, including multi-line selections.",
      created_at: 1_718_450_400_000_000,
    },
    {
      id: "reply-2",
      author_kind: "user",
      created_by: null,
      body: "What about two comments with the same start?",
      created_at: 1_718_450_460_000_000,
    },
    {
      id: "reply-3",
      author_kind: "agent",
      created_by: null,
      body: "Ties keep creation order.",
      created_at: 1_718_450_520_000_000,
    },
    {
      id: "reply-4",
      author_kind: "user",
      created_by: "reviewer@example.com",
      body: "A short comment in the code would help the next reader.",
      created_at: 1_718_450_580_000_000,
    },
  ],
);

export const ReplyThread: Story = {
  parameters: { threadDemo: true },
  args: {
    comments: [
      comment({
        id: "thread-comment",
        reply_count: 4,
        body: "Why sort by character offset rather than line number?",
      }),
    ],
    addressedComments: [],
    activeSelection: {
      start_index: 0,
      end_index: 10,
      comment_id: "thread-comment",
      anchor_content: "def compute_totals(rows):",
    },
  },
  decorators: [
    (Story) => (
      <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, comment_replies: true }}>
        <QueryClientProvider client={threadClient}>
          <CommentSenderProvider sessionId="conversation-story" agentId="agent-story">
            <Story />
          </CommentSenderProvider>
        </QueryClientProvider>
      </CapabilitiesProvider>
    ),
  ],
  play: async ({ canvasElement }) => {
    const input = within(canvasElement).getByRole("textbox", { name: "Reply to comment" });
    await userEvent.click(input);
    await userEvent.type(input, "Could you add that explanation?");
  },
};
