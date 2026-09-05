import type { Meta, StoryObj } from "@storybook/react-vite";
import { PoweredByOmnigent } from "./PoweredByOmnigent";

const meta = {
  title: "Components/PoweredByOmnigent",
  component: PoweredByOmnigent,
  tags: ["visual-snapshot"],
} satisfies Meta<typeof PoweredByOmnigent>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {};
