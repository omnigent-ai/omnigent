import type { Meta, StoryObj } from "@storybook/react-vite";
import type { ComponentType } from "react";
import { userEvent, within } from "storybook/test";

import { childSessionsQueryKey, type ChildSessionInfo } from "@/hooks/useChildSessions";
import type { BackgroundTaskInfo } from "@/lib/types";
import { ChatStoreSeed, StoryQueryRouter } from "@/storybook/StoryProviders";
import { BackgroundTaskIndicator } from "./BackgroundTaskIndicator";
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

function backgroundSeed(count: number, tasks: BackgroundTaskInfo[]) {
  return (Story: ComponentType) => (
    <ChatStoreSeed seed={{ backgroundTaskCount: count, backgroundTasks: tasks, conversationId }}>
      <Story />
    </ChatStoreSeed>
  );
}

function compactIndicatorEnvironment(Story: ComponentType) {
  return (
    <ChatStoreSeed
      seed={{
        backgroundTaskCount: 5,
        backgroundTasks: [{ id: "server", description: "Development server" }],
        conversationId,
      }}
    >
      <StoryQueryRouter
        route={`/c/${conversationId}`}
        seed={(queryClient) => {
          queryClient.setQueryData(childSessionsQueryKey(conversationId), [
            child({ id: "child-active", task_summary: "Verify task controls", busy: true }),
            child({
              id: "child-active-2",
              task_summary: "Review task output",
              busy: true,
            }),
            child({
              id: "child-active-3",
              task_summary: "Check visual snapshots",
              busy: true,
            }),
            child({ id: "child-active-4", task_summary: "Run focused tests", busy: true }),
            child({ id: "child-active-5", task_summary: "Update PR notes", busy: true }),
          ]);
        }}
      >
        <Story />
      </StoryQueryRouter>
    </ChatStoreSeed>
  );
}

function CompactIndicatorRow({ narrow = false }: { narrow?: boolean }) {
  return (
    <div
      data-labels="collapsed"
      className={
        narrow
          ? "group/composer-workspace flex w-28 items-center justify-end gap-1 overflow-hidden rounded-lg border bg-card px-2 py-1.5"
          : "group/composer-workspace flex items-center justify-end gap-1 rounded-lg border bg-card px-2 py-1.5"
      }
    >
      <BackgroundTaskIndicator />
      <SubagentTaskIndicator conversationId={conversationId} />
      <ComposerContextRing contextWindow={200_000} tokensUsed={narrow ? 191_000 : 83_400} />
    </div>
  );
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

export const BackgroundTaskDetails: Story = {
  decorators: [
    backgroundSeed(3, [
      {
        id: "dev-server",
        description: "Vite development server",
        command: "pnpm --filter web dev",
      },
      { id: "focused-tests", description: "Focused composer tests", command: "pnpm vitest" },
    ]),
  ],
  render: () => <BackgroundTaskIndicator />,
  play: ({ canvasElement }) => openIndicator(canvasElement, "background-task-pill"),
};

export const BackgroundTaskCountOnly: Story = {
  decorators: [backgroundSeed(2, [])],
  render: () => <BackgroundTaskIndicator />,
  play: ({ canvasElement }) => openIndicator(canvasElement, "background-task-pill"),
};

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
  decorators: [compactIndicatorEnvironment],
  render: () => <CompactIndicatorRow />,
};

export const ContextRingNarrow: Story = {
  decorators: [compactIndicatorEnvironment],
  render: () => <CompactIndicatorRow narrow />,
};
