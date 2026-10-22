import type { Meta, StoryObj } from "@storybook/react-vite";
import { RunningDot } from "./RunningDot";

const meta = {
  title: "Components/RunningDot",
  component: RunningDot,
  tags: ["visual-snapshot"],
} satisfies Meta<typeof RunningDot>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {};

export const Large: Story = {
  args: {
    className: "size-8",
  },
};
