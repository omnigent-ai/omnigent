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
    onSelect: () => undefined,
    onClose: () => undefined,
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
          queryClient.setQueryData(
            ["host-worktrees", workspaceStoryHost, workspaceStoryProjects],
            [
              {
                path: workspaceStoryProjects,
                branch: "main",
                is_main: true,
                detached: false,
              },
              {
                path: `${workspaceStoryHome}/worktrees/agentic-layouts`,
                branch: "agentic/layouts",
                is_main: false,
                detached: false,
              },
            ],
          );
        }}
      >
        <div className="h-[min(35rem,calc(100dvh-2rem))] w-[min(720px,calc(100vw-2rem))]">
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

export const FullTwoPane: Story = {};

export const CompactEmbedded: Story = {
  args: {
    onSelect: undefined,
    onClose: undefined,
    onNavigate: () => undefined,
  },
  decorators: [
    (Story) => (
      <div className="w-[min(28rem,calc(100vw-2rem))]">
        <Story />
      </div>
    ),
  ],
};

export const TypedFilter: Story = {
  play: async ({ canvasElement }) => {
    const input = within(canvasElement).getByTestId("workspace-picker-search-input");
    await userEvent.clear(input);
    await userEvent.type(input, "ap");
  },
};
