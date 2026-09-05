import type { Meta, StoryObj } from "@storybook/react-vite";
import { userEvent, within } from "storybook/test";
import type { WorkspaceChangedFile, WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { FolderTree } from "./FolderTree";

const file = (path: string, bytes: number): WorkspaceFile => ({
  path,
  name: path.split("/").at(-1) ?? path,
  type: "file",
  bytes,
  modified_at: null,
});

const changed = (
  path: string,
  status: WorkspaceChangedFile["status"],
  bytes: number | null,
): WorkspaceChangedFile => ({
  path,
  name: path.split("/").at(-1) ?? path,
  status,
  bytes,
  modified_at: null,
  lines_added: null,
  lines_removed: null,
});

const meta = {
  title: "Components/Shell/FolderTree",
  component: FolderTree,
  tags: ["visual-snapshot"],
  args: {
    isLoading: false,
    isError: false,
    error: null,
    onFileSelect: () => undefined,
    showHidden: false,
    changedFiles: [],
    sort: "alpha",
  },
  decorators: [
    (Story) => (
      <StoryQueryRouter>
        <div className="w-[340px] rounded-lg border bg-card p-1">
          <Story />
        </div>
      </StoryQueryRouter>
    ),
  ],
} satisfies Meta<typeof FolderTree>;

export default meta;
type Story = StoryObj<typeof meta>;

export const TreeWithChanges: Story = {
  args: {
    conversationId: "story-tree-changes",
    files: [
      file("src/App.tsx", 5400),
      file("src/components/Button.tsx", 2048),
      file("src/components/Card.tsx", 985),
      file("src/utils/helpers.ts", 474_000),
      file("README.md", 8192),
      file("old.txt", 120),
      {
        path: "node_modules",
        name: "node_modules",
        type: "directory",
        bytes: null,
        modified_at: null,
      },
    ],
    changedFiles: [
      changed("src/App.tsx", "created", 5400),
      changed("src/components/Button.tsx", "modified", 2048),
      changed("old.txt", "deleted", null),
    ],
  },
};

export const SearchResults: Story = {
  args: {
    conversationId: "story-tree-search",
    files: [],
    searchQuery: "button",
    searchResults: [
      file("src/components/Button.tsx", 2048),
      file("src/components/ButtonGroup.tsx", 1024),
      file("packages/design-system/src/inputs/toggles/IconButton.tsx", 3210),
    ],
    changedFiles: [changed("src/components/Button.tsx", "modified", 2048)],
  },
};

export const ExpandedFolders: Story = {
  args: {
    conversationId: undefined,
    files: [file("src/main.ts", 1200), file("src/lib/utils.ts", 600), file("README.md", 8192)],
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await userEvent.click(canvas.getByRole("button", { name: "src/" }));
    await userEvent.click(canvas.getByRole("button", { name: "lib/" }));
  },
};
