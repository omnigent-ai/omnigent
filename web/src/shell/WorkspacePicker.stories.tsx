import type { Meta, StoryObj } from "@storybook/react-vite";
import { userEvent, within } from "storybook/test";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { WorkspacePickerDialog } from "./WorkspacePickerDialog";
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

function storyBody(canvasElement: HTMLElement) {
  return within(canvasElement.ownerDocument.body);
}

const meta = {
  title: "Components/Workspace/WorkspacePickerDialog",
  component: WorkspacePickerDialog,
  tags: ["visual-snapshot"],
  args: {
    open: true,
    onOpenChange: () => undefined,
    hostId: workspaceStoryHost,
    initialPath: workspaceStoryProjects,
    onConfirm: () => undefined,
  },
  play: async ({ canvasElement }) => {
    await userEvent.click(
      await storyBody(canvasElement).findByTestId("workspace-picker-search-input"),
    );
  },
  decorators: [
    (Story, context) => (
      <StoryQueryRouter
        seed={(queryClient) => {
          seedFilesystem(queryClient, workspaceStoryProjects, projectEntries);
          seedFilesystem(queryClient, "", [
            storyDirectory(`${workspaceStoryHome}/projects`),
            storyDirectory(`${workspaceStoryHome}/Downloads`),
          ]);
          queryClient.setQueryData(
            ["host-worktrees", workspaceStoryHost, workspaceStoryProjects],
            context.name === "Full Single Pane"
              ? []
              : [
                  {
                    path: workspaceStoryProjects,
                    branch: "main",
                    is_main: true,
                    detached: false,
                  },
                  ...(context.name === "Main Checkout Only"
                    ? []
                    : [
                        {
                          path: `${workspaceStoryHome}/worktrees/agentic-layouts`,
                          branch: "agentic/layouts",
                          is_main: false,
                          detached: false,
                          updated_at: 1_700_000_000,
                        },
                        {
                          path: `${workspaceStoryHome}/worktrees/command-palette`,
                          branch: "feature/command-palette",
                          is_main: false,
                          detached: false,
                          updated_at: 1_699_992_800,
                        },
                        {
                          path: `${workspaceStoryHome}/worktrees/streaming-status`,
                          branch: "feature/streaming-status",
                          is_main: false,
                          detached: false,
                          updated_at: 1_699_913_600,
                        },
                      ]),
                ],
          );
        }}
      >
        <Story />
      </StoryQueryRouter>
    ),
  ],
} satisfies Meta<typeof WorkspacePickerDialog>;

export default meta;
type Story = StoryObj<typeof meta>;

export const PopulatedWithConflict: Story = {
  args: {
    workspacePath: `${workspaceStoryProjects}/app`,
    occupancyForPath: (path) => (path === workspaceStoryProjects ? 2 : 0),
  },
};

export const FullTwoPane: Story = {};

export const FullSinglePane: Story = {};

export const MainCheckoutOnly: Story = {};

export const LinkedWorktreeSelected: Story = {
  play: async ({ canvasElement }) => {
    const body = storyBody(canvasElement);
    await userEvent.click(await body.findByRole("radio", { name: "Use worktree command-palette" }));
    await userEvent.click(await body.findByTestId("workspace-picker-search-input"));
  },
};

export const TypedFilter: Story = {
  play: async ({ canvasElement }) => {
    const input = await storyBody(canvasElement).findByTestId("workspace-picker-search-input");
    await userEvent.clear(input);
    await userEvent.type(input, "ap");
  },
};
