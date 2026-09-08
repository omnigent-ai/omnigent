import type { Meta, StoryObj } from "@storybook/react-vite";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { TooltipProvider } from "@/components/ui/tooltip";
import { FileMenuProvider } from "./FileContextMenu";
import { WorkspacePanel } from "./WorkspacePanel";

const noop = () => undefined;
const sessionId = "story-file-viewer-menu";
const meta = {
  title: "Components/Shell/WorkspacePanel",
  component: WorkspacePanel,
  args: {
    conversationId: sessionId,
    width: 520,
    handleProps: { tabIndex: 0 },
    rightRailTab: "files",
    onRightRailTabChange: noop,
    showFilesPanel: true,
    showGithubTab: false,
    showBrowserTab: false,
    changedCount: 0,
    subagentsWorking: 0,
    agentCount: 1,
    rootSessionId: null,
    selectedFilePath: "docs/README.md",
    openFiles: ["docs/README.md", "src/main.ts"],
    openFileViewer: noop,
    onCloseFile: noop,
    onShowScopeView: noop,
    onCommentsOpenChange: noop,
    openTerminalTab: noop,
    openTerminals: [],
    selectedTerminalKey: null,
    onCloseTerminal: noop,
    maximized: false,
    onToggleMaximized: noop,
    permissionLevel: null,
    filesPanelSort: "recent",
    onSortChange: noop,
    filesPanelShowHidden: false,
    onShowHiddenChange: noop,
  },
  decorators: [
    (Story) => (
      <StoryQueryRouter
        seed={(client) => {
          client.setQueryData(["file-content", sessionId, "docs/README.md"], {
            object: "session.environment.filesystem.file_content",
            path: "docs/README.md",
            content_type: "text/markdown",
            encoding: "utf-8",
            content:
              "# Project notes\n\nOpen a file's location or copy its path from the tab, the displayed path, or the menu beside it.\n",
          });
          client.setQueryData(["workspace-changed-files", sessionId], { data: [] });
          client.setQueryData(["comments", sessionId, "docs/README.md"], []);
          client.setQueryData(["session-agent", sessionId], { id: "story-agent", terminals: [] });
        }}
      >
        <TooltipProvider>
          <FileMenuProvider root="/Users/demo/project" hostId="story-local-host">
            <div className="flex h-[450px]">
              <Story />
            </div>
          </FileMenuProvider>
        </TooltipProvider>
      </StoryQueryRouter>
    ),
  ],
} satisfies Meta<typeof WorkspacePanel>;
export default meta;
type Story = StoryObj<typeof meta>;
export const OpenFileMenus: Story = {};
