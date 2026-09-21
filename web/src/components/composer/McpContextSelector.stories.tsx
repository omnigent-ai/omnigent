import type { Meta, StoryObj } from "@storybook/react-vite";

import { McpContextSelector } from "./McpContextSelector";
import { mcpContextOptionsFromServers } from "./mcpContextOptions";

const OPTIONS = mcpContextOptionsFromServers([
  {
    name: "github",
    transport: "http",
    description: "Repository search, issues, and pull requests",
    url: "https://mcp.github.example/api",
  },
  {
    name: "local-files",
    transport: "stdio",
    description: "Read files from the selected workspace",
    command: "/opt/omnigent/bin/files-mcp",
  },
  {
    name: "team-knowledge-with-a-long-server-name",
    transport: "http",
    description: "Search internal docs and long-form engineering references",
    url: "https://knowledge.example.com/mcp",
  },
]);

const meta = {
  title: "Composer/McpContextSelector",
  component: McpContextSelector,
  tags: ["visual-snapshot"],
  parameters: { layout: "centered" },
  args: {
    open: true,
    onOpenChange: () => undefined,
    onChange: () => undefined,
    resource: { status: "ready", data: OPTIONS, error: null },
    value: [
      { id: "github", serverName: "github" },
      { id: "local-files", serverName: "local-files" },
    ],
  },
  decorators: [
    (Story) => (
      <div className="min-h-96 w-[28rem] max-w-[calc(100vw-2rem)] p-8">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof McpContextSelector>;

export default meta;
type Story = StoryObj<typeof meta>;

export const SelectedServers: Story = {};

export const IntentionallyNone: Story = {
  args: { value: [] },
};

export const NarrowWidth: Story = {
  decorators: [
    (Story) => (
      <div className="min-h-96 w-64 p-4">
        <Story />
      </div>
    ),
  ],
  args: {
    value: [
      { id: "github", serverName: "github" },
      {
        id: "team-knowledge-with-a-long-server-name",
        serverName: "team-knowledge-with-a-long-server-name",
      },
    ],
  },
};

export const Loading: Story = {
  args: { resource: { status: "loading", data: null, error: null }, value: [] },
};

export const Unavailable: Story = {
  args: { resource: { status: "unavailable", data: null, error: null }, value: [] },
};

export const ErrorState: Story = {
  args: {
    resource: {
      status: "error",
      data: null,
      error: new Error("The agent metadata request failed"),
    },
    value: [{ id: "github", serverName: "github" }],
    onRetry: () => undefined,
  },
};
