import type { Meta, StoryObj } from "@storybook/react-vite";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import { FileMentionMenu } from "./FileMentionMenu";

const rootEntries: WorkspaceFile[] = [
  { path: "src", name: "src", type: "directory", bytes: null, modified_at: null },
  { path: "tests", name: "tests", type: "directory", bytes: null, modified_at: null },
  { path: "README.md", name: "README.md", type: "file", bytes: 8192, modified_at: null },
  { path: "package.json", name: "package.json", type: "file", bytes: 2400, modified_at: null },
];

const nestedEntries: WorkspaceFile[] = [
  {
    path: "src/components",
    name: "components",
    type: "directory",
    bytes: null,
    modified_at: null,
  },
  { path: "src/lib", name: "lib", type: "directory", bytes: null, modified_at: null },
  { path: "src/App.tsx", name: "App.tsx", type: "file", bytes: 5400, modified_at: null },
];

const meta = {
  title: "Components/Menus/FileMentionMenu",
  component: FileMentionMenu,
  tags: ["visual-snapshot"],
  args: {
    onOpenDir: () => undefined,
    onAttach: () => undefined,
  },
  decorators: [
    (Story) => (
      <div className="relative h-[360px] w-[380px] rounded-xl border bg-card">
        <div className="absolute inset-x-4 bottom-4 h-12 rounded-xl border bg-background">
          <Story />
        </div>
      </div>
    ),
  ],
} satisfies Meta<typeof FileMentionMenu>;

export default meta;
type Story = StoryObj<typeof meta>;

export const WorkspaceRoot: Story = {
  args: {
    currentDir: "",
    activeIndex: 0,
    entries: rootEntries,
  },
};

export const NestedDirectory: Story = {
  args: {
    currentDir: "src",
    activeIndex: 2,
    entries: nestedEntries,
  },
};

export const Loading: Story = {
  args: {
    currentDir: "src/components",
    activeIndex: -1,
    entries: [],
    loading: true,
  },
};
