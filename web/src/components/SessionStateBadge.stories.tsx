import type { Meta, StoryObj } from "@storybook/react-vite";
import { SessionStateBadge } from "./SessionStateBadge";

const meta = {
  title: "Components/SessionStateBadge",
  component: SessionStateBadge,
  tags: ["visual-snapshot"],
} satisfies Meta<typeof SessionStateBadge>;

export default meta;
type Story = StoryObj<typeof meta>;

export const AwaitingOneApproval: Story = {
  args: { state: { kind: "awaiting", count: 1 } },
};

export const AwaitingSeveralApprovals: Story = {
  args: { state: { kind: "awaiting", count: 3 } },
};

export const Running: Story = {
  args: { state: { kind: "running" } },
};

export const Starting: Story = {
  args: { state: { kind: "starting" } },
};

export const UnseenMessages: Story = {
  args: { state: { kind: "unseen" } },
};
