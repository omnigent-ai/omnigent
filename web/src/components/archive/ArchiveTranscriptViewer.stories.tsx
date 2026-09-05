import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, within } from "storybook/test";

import type { Conversation } from "@/hooks/useConversations";
import type { ConversationItem } from "@/lib/conversationItems";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { ArchiveTranscriptViewer } from "./ArchiveTranscriptViewer";

const conversation: Conversation = {
  id: "archive-session-with-a-long-synthetic-identifier",
  object: "conversation",
  title: "Review mobile archive layout and long technical notes",
  agent_name: "Example native agent",
  created_at: 1_780_000_000,
  updated_at: 1_780_000_200,
  labels: {},
  permission_level: 3,
  archived: true,
};

const items: ConversationItem[] = [
  {
    id: "archive-user-1",
    response_id: "archive-response-1",
    type: "message",
    role: "user",
    status: "completed",
    content: [{ type: "input_text", text: "Please check the archived notes on a phone." }],
  },
  {
    id: "archive-answer-1",
    response_id: "archive-response-1",
    type: "message",
    role: "assistant",
    status: "completed",
    model: "Example agent",
    content: [
      {
        type: "output_text",
        text: [
          "The review is complete. The archive keeps the original conversation formatting.",
          "",
          "Long paths such as `/workspace/example-project/docs/reports/archive-layout-verification.md` stay readable inside the available width.",
          "",
          "中文正文也需要按照手机宽度自动换行，不应把搜索框或右侧操作按钮挤出屏幕。",
          "",
          "```typescript",
          'const report = { status: "complete", source: "example-project", preserveSearch: true };',
          "```",
          "",
          "| Check | Result |",
          "| --- | --- |",
          "| Header and search | Within the reader |",
          "| Conversation text | Wraps on narrow screens |",
          "| Turn navigation | Preserved |",
        ].join("\n"),
      },
    ],
  },
  {
    id: "archive-user-2",
    response_id: "archive-response-2",
    type: "message",
    role: "user",
    status: "completed",
    content: [
      { type: "input_text", text: "Keep search, citations and turn navigation available." },
    ],
  },
  {
    id: "archive-answer-2",
    response_id: "archive-response-2",
    type: "message",
    role: "assistant",
    status: "completed",
    model: "Example agent",
    content: [{ type: "output_text", text: "All existing archive actions remain available." }],
  },
];

const meta = {
  title: "Components/Archive/TranscriptViewer",
  component: ArchiveTranscriptViewer,
  tags: ["visual-snapshot"],
  args: { conversation },
  render: (args) => <ArchiveTranscriptViewer {...args} onBack={() => undefined} />,
  decorators: [
    (Story, context) => (
      <StoryQueryRouter
        route="/settings/archived"
        seed={(client) => {
          client.setQueryData(["archive-transcript", conversation.id], {
            pages: [{ items, hasMore: false }],
            pageParams: [undefined],
          });
        }}
      >
        <div className="fixed inset-0 flex items-center justify-center overflow-hidden bg-background">
          <div
            data-testid="archive-reader-story-frame"
            className="flex h-full max-h-[700px] max-w-full overflow-hidden border bg-background"
            style={{ width: context.parameters.readerWidth ?? 390 }}
          >
            <Story />
          </div>
        </div>
      </StoryQueryRouter>
    ),
  ],
} satisfies Meta<typeof ArchiveTranscriptViewer>;

export default meta;
type Story = StoryObj<typeof meta>;

export const MobileLongContent: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await canvas.findByText("Please check the archived notes on a phone.");
    const frame = canvas.getByTestId("archive-reader-story-frame");
    const reader = canvas.getByTestId("archive-transcript-viewer");
    const scroller = canvas.getByTestId("archive-transcript");
    expect(reader.getBoundingClientRect().width).toBeLessThanOrEqual(frame.clientWidth + 1);
    expect(scroller.scrollWidth).toBeLessThanOrEqual(scroller.clientWidth + 1);
    for (const label of ["Copy session ID", "Copy session link", "Open full session"]) {
      const action = canvas.getByRole("button", { name: label });
      expect(action.getBoundingClientRect().right).toBeLessThanOrEqual(
        frame.getBoundingClientRect().right + 1,
      );
    }
  },
};

export const DesktopLongContent: Story = { parameters: { readerWidth: 720 } };
