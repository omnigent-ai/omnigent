import type { Meta, StoryObj } from "@storybook/react-vite";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { BrowseLocationBar } from "./BrowseLocationBar";
import { workspaceStoryHost } from "./workspaceStoryFixtures";

const workspace = "/Users/story/projects/app";

const meta = {
  title: "Components/Workspace/BrowseLocationBar",
  component: BrowseLocationBar,
  tags: ["visual-snapshot"],
  args: { current: workspace, workspace, hostId: workspaceStoryHost, onNavigate: () => undefined },
  decorators: [
    (Story) => (
      <StoryQueryRouter>
        <div className="w-[520px] rounded-xl border bg-card p-3">
          <Story />
        </div>
      </StoryQueryRouter>
    ),
  ],
} satisfies Meta<typeof BrowseLocationBar>;

export default meta;
type Story = StoryObj<typeof meta>;

export const RoamableWithError: Story = {
  args: {
    canBrowseOutside: true,
    reach: { unconfined: true, roots: [] },
    error: "Couldn't list /etc — the runner refused the path.",
  },
};

export const ConfinedViewer: Story = {
  args: {
    canBrowseOutside: false,
    reach: {
      unconfined: false,
      roots: [{ path: workspace, access: "read", origin: "cwd" }],
    },
  },
};
