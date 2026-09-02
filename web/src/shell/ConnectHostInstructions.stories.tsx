import type { Meta, StoryObj } from "@storybook/react-vite";
import { userEvent, within } from "storybook/test";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
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
  decorators: [
    (Story) => (
      <CapabilitiesProvider info={FALLBACK_SERVER_INFO}>
        <Story />
      </CapabilitiesProvider>
    ),
  ],
};

export const DatabricksLakeboxTab: Story = {
  args: { serverUrl: "https://omni.internal.example.com" },
  decorators: [
    (Story) => (
      <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, databricks_features: true }}>
        <Story />
      </CapabilitiesProvider>
    ),
  ],
  play: async ({ canvasElement }) => {
    await userEvent.click(within(canvasElement).getByRole("tab", { name: "Databricks Lakebox" }));
  },
};
