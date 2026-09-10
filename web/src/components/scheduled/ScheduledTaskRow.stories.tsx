import type { Meta, StoryObj } from "@storybook/react-vite";
import { userEvent, within } from "storybook/test";
import type { ScheduledTask } from "@/lib/scheduledTasksApi";
import { ScheduledTaskRow } from "./ScheduledTaskRow";

const now = new Date("2026-03-10T12:00:00Z");

function task(overrides: Partial<ScheduledTask> = {}): ScheduledTask {
  return {
    id: "scheduled-story",
    name: "Weekday repository triage",
    prompt: "Review new issues and prepare a concise triage report.",
    rrule: "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=8;BYMINUTE=0",
    ownerUserId: null,
    agentId: "agent-reviewer",
    timezone: "UTC",
    createdAt: 1,
    updatedAt: 2,
    modelOverride: null,
    reasoningEffort: null,
    permissionMode: null,
    workspace: null,
    hostId: null,
    state: "active",
    lastRunAt: null,
    lastRunStatus: null,
    lastRunConversationId: null,
    nextRunAt: "2026-03-10T14:01:00Z",
    ...overrides,
  };
}

const meta = {
  title: "Components/Scheduled/ScheduledTaskRow",
  component: ScheduledTaskRow,
  tags: ["visual-snapshot"],
  args: {
    now,
    onEdit: () => undefined,
    onPauseToggle: () => undefined,
    onRunNow: () => undefined,
    onDelete: () => undefined,
    busy: false,
  },
  decorators: [
    (Story) => (
      <div className="w-[620px]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof ScheduledTaskRow>;

export default meta;
type Story = StoryObj<typeof meta>;

export const ActiveWithNextRun: Story = {
  args: {
    task: task({
      name: "Review repository issues and prepare the weekday triage report",
    }),
  },
};

export const PausedMenuOpen: Story = {
  args: {
    task: task({ state: "paused", nextRunAt: null }),
  },
  play: async ({ canvasElement }) => {
    await userEvent.click(within(canvasElement).getByTestId("task-row-menu"));
  },
};
