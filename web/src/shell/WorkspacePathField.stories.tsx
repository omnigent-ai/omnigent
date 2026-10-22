import type { Meta, StoryObj } from "@storybook/react-vite";
import { useState } from "react";
import { userEvent, within } from "storybook/test";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { WorkspacePathField } from "./WorkspacePathField";
import {
  seedFilesystem,
  storyDirectory,
  workspaceStoryHome,
  workspaceStoryHost,
} from "./workspaceStoryFixtures";

function PathFieldExample({ initialValue, recent }: { initialValue: string; recent: string[] }) {
  const [value, setValue] = useState(initialValue);
  return (
    <WorkspacePathField
      hostId={workspaceStoryHost}
      value={value}
      onChange={setValue}
      onBrowse={() => undefined}
      onCommit={() => undefined}
      recent={recent}
    />
  );
}

const meta = {
  title: "Components/Workspace/WorkspacePathField",
  tags: ["visual-snapshot"],
  decorators: [
    (Story) => (
      <StoryQueryRouter
        seed={(queryClient) => {
          seedFilesystem(queryClient, "", [
            storyDirectory(`${workspaceStoryHome}/projects`),
            storyDirectory(`${workspaceStoryHome}/prototypes`),
            storyDirectory(`${workspaceStoryHome}/documents`),
          ]);
          seedFilesystem(queryClient, "/", []);
        }}
      >
        <div className="w-[520px] rounded-xl border bg-card p-4">
          <Story />
        </div>
      </StoryQueryRouter>
    ),
  ],
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

export const RecentAndMatches: Story = {
  render: () => (
    <PathFieldExample
      initialValue="pro"
      recent={[`${workspaceStoryHome}/projects/app`, `${workspaceStoryHome}/notes`]}
    />
  ),
  play: async ({ canvasElement }) => {
    await userEvent.click(within(canvasElement).getByTestId("workspace-path-input"));
    await userEvent.keyboard("{ArrowDown}");
  },
};

export const AbsolutePath: Story = {
  render: () => <PathFieldExample initialValue="/tmp" recent={[]} />,
  play: async ({ canvasElement }) => {
    await userEvent.click(within(canvasElement).getByTestId("workspace-path-input"));
  },
};
