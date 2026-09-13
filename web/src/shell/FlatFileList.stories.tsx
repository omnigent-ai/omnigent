import type { Meta, StoryObj } from "@storybook/react-vite";
import { RunnerOfflineError, type WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";
import { FlatFileList } from "./FlatFileList";

const changed = (
  path: string,
  status: WorkspaceChangedFile["status"],
  overrides: Partial<WorkspaceChangedFile> = {},
): WorkspaceChangedFile => ({
  path,
  name: path.split("/").at(-1) ?? path,
  status,
  bytes: status === "deleted" ? null : 2048,
  modified_at: 1_700_000_000,
  lines_added: null,
  lines_removed: null,
  ...overrides,
});

const meta = {
  title: "Components/Shell/FlatFileList",
  component: FlatFileList,
  tags: ["visual-snapshot"],
  args: {
    isLoading: false,
    isError: false,
    error: null,
    onFileSelect: () => undefined,
    showHidden: false,
    onShowHidden: () => undefined,
    searchQuery: "",
    sort: "alpha",
    conversationId: "story-flat-files",
  },
  decorators: [
    (Story) => (
      <div className="w-[340px] rounded-lg border bg-card p-1">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof FlatFileList>;

export default meta;
type Story = StoryObj<typeof meta>;

export const ChangedFilesWithHiddenBanner: Story = {
  args: {
    files: [
      changed("src/hooks/useThing.ts", "modified", { lines_added: 1204, lines_removed: 318 }),
      changed("src/App.tsx", "created", { bytes: 5400, lines_added: 96 }),
      changed("docs/old-notes.md", "deleted", { lines_removed: 42 }),
      changed(".env.local", "modified", { bytes: 64, lines_added: 1, lines_removed: 1 }),
    ],
  },
};

export const RunnerAsleep: Story = {
  args: {
    files: undefined,
    isError: true,
    error: new RunnerOfflineError(),
    runnerWentOffline: true,
  },
};
