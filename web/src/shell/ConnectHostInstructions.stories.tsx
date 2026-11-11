import type { Meta, StoryObj } from "@storybook/react-vite";
import { ConnectHostInstructions } from "./NewChatDialog";

const meta = {
  title: "Components/Hosts/ConnectHostInstructions",
  component: ConnectHostInstructions,
  tags: ["visual-snapshot"],
  args: {
    label: "Connect a host to run sessions on your own machine.",
  },
  decorators: [
    (Story) => (
      <div className="w-[680px] bg-card">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof ConnectHostInstructions>;

export default meta;
type Story = StoryObj<typeof meta>;

export const OssSingleCommand: Story = {
  args: { serverUrl: "https://omni.example.com" },
};
