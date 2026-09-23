import type { Meta, StoryObj } from "@storybook/react-vite";
import type { QueuedMessage } from "@/store/chatStore";
import { QueuedMessagesStrip } from "@/pages/QueuedMessagesStrip";
import { ComposerWorkspaceBar } from "./ComposerControls";
import { ComposerWorkspaceStatus } from "./ComposerWorkspaceStatus";

const meta = {
  title: "Components/Composer/ComposerWorkspaceStatus",
  component: ComposerWorkspaceStatus,
  tags: ["visual-snapshot"],
  args: {
    workspacePath: "/Users/dev/projects/omnigent",
    worktreePath: null,
    isWorktree: null,
    branch: null,
    branchState: "unknown",
    creationBranch: null,
    showWorktree: false,
  },
  decorators: [
    (Story) => (
      <div className="w-[680px] rounded-2xl bg-muted/30 p-6">
        <ComposerWorkspaceBar>
          <Story />
        </ComposerWorkspaceBar>
      </div>
    ),
  ],
} satisfies Meta<typeof ComposerWorkspaceStatus>;

export default meta;
type Story = StoryObj<typeof meta>;

export const WorkspacePathOnly: Story = {};

export const WorktreeAndBranch: Story = {
  args: {
    workspacePath: "/Users/dev/projects/omnigent",
    worktreePath: "/Users/dev/projects/omnigent-worktrees/composer-layout",
    isWorktree: true,
    branch: "composer-layout",
    branchState: "branch",
    creationBranch: "main",
    showWorktree: true,
  },
};

const QUEUED: QueuedMessage[] = [
  { queueId: "queue-1", text: "Rebase the layout branch once CI is green", conversationId: "c1" },
  { queueId: "queue-2", text: "Then re-run the geometry contract suite", conversationId: "c1" },
];

/** The bar's nesting variant when a queued strip docks above it (the strip's
 * tray owns the rounded top; the bar drops its top border and radius). */
export const QueuedPresentNesting: Story = {
  decorators: [
    (Story) => (
      <div className="w-[680px] rounded-2xl bg-muted/30 p-6">
        <QueuedMessagesStrip
          messages={QUEUED}
          onDelete={() => undefined}
          onEdit={() => undefined}
          widthClassName="mx-3 w-auto"
        />
        <ComposerWorkspaceBar className="rounded-t-none border-t-0 border-border/50 before:pointer-events-none before:absolute before:inset-x-4 before:top-0 before:h-px before:bg-border/50 before:content-['']">
          <Story />
        </ComposerWorkspaceBar>
      </div>
    ),
  ],
};
