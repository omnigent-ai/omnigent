import type { Meta, StoryObj } from "@storybook/react-vite";
import { userEvent, within } from "storybook/test";

import { childSessionsQueryKey, type ChildSessionInfo } from "@/hooks/useChildSessions";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { ComposerContextRing } from "./ComposerContextRing";
import { SubagentTaskIndicator } from "./SubagentTaskIndicator";

const conversationId = "conversation-story";

function child(overrides: Partial<ChildSessionInfo> & { id: string }): ChildSessionInfo {
  return {
    title: null,
    task_summary: null,
    tool: null,
    session_name: null,
    labels: {},
    current_task_status: null,
    last_task_error: null,
    busy: false,
    last_message_preview: null,
    pending_elicitations_count: 0,
    routed_model: null,
    ...overrides,
  };
}

async function openIndicator(canvasElement: HTMLElement, testId: string) {
  await userEvent.click(within(canvasElement).getByTestId(testId));
}

const meta = {
  title: "Components/Composer/Indicators",
  tags: ["visual-snapshot"],
  decorators: [
    (Story) => (
      <div className="flex min-h-52 w-[440px] items-end justify-end rounded-xl border bg-background p-5">
        <Story />
      </div>
    ),
  ],
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

export const SubagentStatesAndNavigation: Story = {
  decorators: [
    (Story) => (
      <StoryQueryRouter
        route={`/c/${conversationId}?file=README.md&focus=agents`}
        seed={(queryClient) => {
          queryClient.setQueryData(childSessionsQueryKey(conversationId), [
            child({
              id: "child-active",
              task_summary: "Build active task indicators",
              tool: "frontend-engineer",
              busy: true,
            }),
            child({
              id: "child-parked",
              task_summary: "Confirm the interaction copy",
              tool: "product-reviewer",
              pending_elicitations_count: 1,
            }),
            child({
              id: "child-error",
              task_summary: "Verify the visual snapshots",
              tool: "test-engineer",
              current_task_status: "failed",
              last_task_error: { code: "snapshot_error", message: "Snapshot mismatch" },
            }),
          ]);
        }}
      >
        <Story />
      </StoryQueryRouter>
    ),
  ],
  render: () => <SubagentTaskIndicator conversationId={conversationId} />,
  play: ({ canvasElement }) => openIndicator(canvasElement, "subagent-task-pill"),
};

export const ContextRingCompact: Story = {
  render: () => (
    <div className="flex items-center rounded-lg border bg-card px-2 py-1.5">
      <ComposerContextRing contextWindow={200_000} tokensUsed={83_400} />
    </div>
  ),
};

export const ContextRingNarrow: Story = {
  render: () => (
    <div
      data-labels="collapsed"
      className="group/composer-workspace flex w-20 items-center justify-end overflow-hidden rounded-lg border bg-card px-2 py-1.5"
    >
      <ComposerContextRing contextWindow={200_000} tokensUsed={191_000} />
    </div>
  ),
};
