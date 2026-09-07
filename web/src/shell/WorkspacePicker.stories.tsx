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
  play: async ({ canvasElement }) => {
    const input = within(canvasElement).getByTestId("workspace-picker-path-input");
    await userEvent.clear(input);
    await userEvent.type(input, `${workspaceStoryProjects}/ap`);
  },
};
