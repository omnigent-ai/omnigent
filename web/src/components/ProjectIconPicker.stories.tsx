import type { Meta, StoryObj } from "@storybook/react-vite";
import { userEvent, within } from "storybook/test";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { ProjectLandingIcon } from "./ProjectIconPicker";

const projectConfig = {
  host_id: "host-story",
  workspace: "/workspace/omnigent",
  agent_id: "agent-story",
};

const meta = {
  title: "Components/Branding/ProjectLandingIcon",
  component: ProjectLandingIcon,
  tags: ["visual-snapshot"],
  args: {
    projectId: "project-story",
    projectName: "Design system",
    configReady: true,
  },
  decorators: [
    (Story) => (
      <StoryQueryRouter>
        <div className="flex min-h-40 w-[360px] items-center justify-center rounded-xl border bg-card">
          <Story />
        </div>
      </StoryQueryRouter>
    ),
  ],
} satisfies Meta<typeof ProjectLandingIcon>;

export default meta;
type Story = StoryObj<typeof meta>;

export const FolderDefault: Story = {
  args: { config: projectConfig },
};

export const EmojiSet: Story = {
  args: { config: { ...projectConfig, icon: "🔥" } },
};

export const EmojiActionsVisible: Story = {
  args: { config: { ...projectConfig, icon: "🧭" } },
  play: async ({ canvasElement }) => {
    await userEvent.tab();
    const edit = within(canvasElement).getByRole("button", { name: "Change project icon" });
    edit.focus();
  },
};
