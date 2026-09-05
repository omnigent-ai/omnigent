import type { Meta, StoryObj } from "@storybook/react-vite";
import { userEvent, within } from "storybook/test";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { WorkspacePicker } from "./WorkspacePicker";
import {
  seedFilesystem,
  storyDirectory,
  storyFile,
  workspaceStoryHome,
  workspaceStoryHost,
  workspaceStoryProjects,
} from "./workspaceStoryFixtures";

const projectEntries = [
  storyDirectory(`${workspaceStoryProjects}/api`),
  storyDirectory(`${workspaceStoryProjects}/app`),
  storyDirectory(`${workspaceStoryProjects}/ml experiments`),
  storyDirectory(`${workspaceStoryProjects}/.git`),
  storyFile(`${workspaceStoryProjects}/README.md`, 2048),
];

const meta = {
  title: "Components/Workspace/WorkspacePicker",
  component: WorkspacePicker,
  tags: ["visual-snapshot"],
  args: {
    hostId: workspaceStoryHost,
    initialPath: workspaceStoryProjects,
    defaultPath: workspaceStoryProjects,
    defaultPathHostName: "MacBook Pro",
    onDefaultPathChange: () => undefined,
    onSelect: () => undefined,
  },
  decorators: [
    (Story) => (
      <StoryQueryRouter
        seed={(queryClient) => {
          seedFilesystem(queryClient, workspaceStoryProjects, projectEntries);
          seedFilesystem(queryClient, "", [
            storyDirectory(`${workspaceStoryHome}/projects`),
            storyDirectory(`${workspaceStoryHome}/Downloads`),
          ]);
        }}
      >
        <div className="w-[440px] rounded-xl border bg-card p-2">
          <Story />
        </div>
      </StoryQueryRouter>
    ),
  ],
} satisfies Meta<typeof WorkspacePicker>;

export default meta;
type Story = StoryObj<typeof meta>;

export const PopulatedWithConflict: Story = {
  args: {
    onClose: () => undefined,
    workspacePath: `${workspaceStoryProjects}/app`,
    occupancyForPath: (path) => (path === workspaceStoryProjects ? 2 : 0),
  },
};

export const TypedFilter: Story = {
  // Keep the export key stable so the existing visual-baseline id remains
  // stable; the user-facing story name reflects the now-separate search UI.
  name: "Folder search",
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await userEvent.type(canvas.getByTestId("workspace-picker-search-input"), "ap");
  },
};
