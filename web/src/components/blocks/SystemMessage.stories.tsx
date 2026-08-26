import type { Meta, StoryObj } from "@storybook/react-vite";
import { SystemMessageView } from "./SystemMessage";

const meta = {
  title: "Components/Blocks/SystemMessage",
  component: SystemMessageView,
  tags: ["visual-snapshot"],
} satisfies Meta<typeof SystemMessageView>;

export default meta;
type Story = StoryObj<typeof meta>;

export const TaskCompleted: Story = {
  args: {
    message: {
      kind: "task_completed",
      label: "Sub-agent research completed",
      body: "The requested repository scan completed successfully.",
    },
  },
};

export const TaskFailed: Story = {
  args: {
    message: {
      kind: "task_failed",
      label: "Tool dependency-check failed",
      body: "The package registry could not be reached.",
    },
  },
};

export const TimerFired: Story = {
  args: {
    message: {
      kind: "timer_fired",
      label: "Timer follow-up fired",
      body: "Check whether the deployment has completed.",
    },
  },
};

export const Interrupted: Story = {
  args: {
    message: {
      kind: "interrupted",
      label: "Interrupted",
      body: "",
    },
  },
};
