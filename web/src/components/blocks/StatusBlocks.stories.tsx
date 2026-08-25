import type { Meta, StoryObj } from "@storybook/react-vite";
import { CompactionMarker, PolicyDeniedBanner, RetryIndicator } from "./StatusBlocks";

const meta = {
  title: "Components/Blocks/StatusIndicators",
  tags: ["visual-snapshot"],
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

export const PolicyDenied: Story = {
  render: () => (
    <PolicyDeniedBanner
      phase="before tool call"
      reason="Writing outside the selected workspace is not allowed."
    />
  ),
};

export const RetryingImmediately: Story = {
  render: () => <RetryIndicator source="runner" attempt={2} maxAttempts={4} delaySeconds={0} />,
};

export const RetryingAfterDelay: Story = {
  render: () => (
    <RetryIndicator source="model stream" attempt={2} maxAttempts={4} delaySeconds={1.5} />
  ),
};

export const Compacted: Story = {
  render: () => <CompactionMarker />,
};
