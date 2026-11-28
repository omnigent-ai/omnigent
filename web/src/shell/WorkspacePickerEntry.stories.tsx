import type { Meta, StoryObj } from "@storybook/react-vite";

import { WorkspacePickerEntry } from "./WorkspacePickerEntry";

const meta = {
  title: "Components/Workspace/WorkspacePickerEntry",
  component: WorkspacePickerEntry,
  tags: ["visual-snapshot"],
  args: {
    entry: {
      name: "omni-omni",
      path: "/Users/ajay/Desktop/omnigent-working-dir/omni-omni",
      type: "directory",
      bytes: null,
      modified_at: 0,
    },
    onOpen: () => undefined,
  },
  decorators: [
    (Story) => (
      <div className="w-[min(44rem,calc(100vw-2rem))] overflow-hidden rounded-md border bg-background py-2">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof WorkspacePickerEntry>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {};

export const Compact: Story = {
  args: { variant: "compact" },
};

export const File: Story = {
  args: {
    entry: {
      name: "README.md",
      path: "/Users/ajay/Desktop/omnigent-working-dir/README.md",
      type: "file",
      bytes: 1024,
      modified_at: 0,
    },
  },
};
