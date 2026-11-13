import { RenderErrorBoundary } from "@/components/RenderErrorBoundary";
import { isOwnerLevel } from "@/lib/permissionsApi";
import { ContentSearchPanel } from "./ContentSearchPanel";
import { ExecutionLogsPanel } from "./ExecutionLogsPanel";
import type { OpenFileOptions } from "./FileViewerContext";
import { FilesPanelDrawer } from "./FilesPanelDrawer";
import type { ChangedSort } from "./FlatFileList";
import { InlineTerminalsSection } from "./InlineTerminalsSection";
import { MobilePanelDrawer } from "./MobilePanelDrawer";
import { PullRequestPanel } from "./PullRequestPanel";
import { SubagentsPanel } from "./SubagentsPanel";
import { TerminalsPanel } from "./TerminalsPanel";

export interface AppShellWorkspacePanelsProps {
  conversationId: string | undefined;
  terminalFirst: boolean;
  panelOpen: boolean;
  panelInitialKey: string | null;
  setPanelInitialKey: (key: string | null) => void;
  permissionLevel: number | null;
  executionLogsOpen: boolean;
  executionLogsKey: string | null;
  setExecutionLogsKey: (key: string | null) => void;
  showFilesPanel: boolean;
  filesPanelOpen: boolean;
  setFilesPanelOpen: (open: boolean) => void;
  openFileViewer: (path: string, options?: OpenFileOptions) => void;
  filesDrawerFlatView: boolean;
  filesPanelShowHidden: boolean;
  setFilesPanelShowHidden: (show: boolean) => void;
  filesPanelSort: ChangedSort;
  handleFilesSortChange: (sort: ChangedSort) => void;
  rootSessionId: string | null;
  subagentsPanelOpen: boolean;
  setSubagentsPanelOpen: (open: boolean) => void;
  shellsPanelOpen: boolean;
  setShellsPanelOpen: (open: boolean) => void;
  openTerminalsPanel: (key: string) => void;
  githubPanelOpen: boolean;
  setGithubPanelOpen: (open: boolean) => void;
  mobileViewport: boolean;
  railTabsAvailable: { readonly "content-search": boolean };
  contentSearchPanelOpen: boolean;
  setContentSearchPanelOpen: (open: boolean) => void;
  openContentSearchResult: (path: string, options?: OpenFileOptions) => void;
  setMobileFileTarget: (element: HTMLDivElement | null) => void;
}

/**
 * The workspace surfaces that sit beside the chat as siblings of the
 * chat+workspace group: the desktop push panels and the mobile drawers for
 * rail tabs that have no push panel of their own.
 */
export function AppShellWorkspacePanels({
  conversationId,
  terminalFirst,
  panelOpen,
  panelInitialKey,
  setPanelInitialKey,
  permissionLevel,
  executionLogsOpen,
  executionLogsKey,
  setExecutionLogsKey,
  showFilesPanel,
  filesPanelOpen,
  setFilesPanelOpen,
  openFileViewer,
  filesDrawerFlatView,
  filesPanelShowHidden,
  setFilesPanelShowHidden,
  filesPanelSort,
  handleFilesSortChange,
  rootSessionId,
  subagentsPanelOpen,
  setSubagentsPanelOpen,
  shellsPanelOpen,
  setShellsPanelOpen,
  openTerminalsPanel,
  githubPanelOpen,
  setGithubPanelOpen,
  mobileViewport,
  railTabsAvailable,
  contentSearchPanelOpen,
  setContentSearchPanelOpen,
  openContentSearchResult,
  setMobileFileTarget,
}: AppShellWorkspacePanelsProps) {
  return (
    <>
      {/* Push panels — flex siblings to main, animate width. Only one is open at a time.
          Terminal-first sessions render the terminal inline inside main
          (via MainTerminalView in ChatPage) and never mount the drawer. */}
      {conversationId && !terminalFirst && (
        <TerminalsPanel
          open={panelOpen}
          conversationId={conversationId}
          initialTerminalKey={panelInitialKey}
          // No neighbor to resize against (chat is hidden, FilesPanel
          // owns its own width) — grow via flex-1.
          fluid={panelOpen}
          // Non-owners attach read-only: a shared PTY can't attribute
          // input per-user, so only the owner may type (server-enforced).
          readOnly={!isOwnerLevel(permissionLevel)}
          onClose={() => setPanelInitialKey(null)}
        />
      )}
      {conversationId && (
        <ExecutionLogsPanel
          open={executionLogsOpen}
          conversationId={conversationId}
          initialKey={executionLogsKey}
          onClose={() => setExecutionLogsKey(null)}
        />
      )}
      {conversationId && showFilesPanel && (
        <FilesPanelDrawer
          open={filesPanelOpen}
          onClose={() => setFilesPanelOpen(false)}
          onFileSelect={openFileViewer}
          flatView={filesDrawerFlatView}
          showHidden={filesPanelShowHidden}
          onShowHiddenChange={setFilesPanelShowHidden}
          sort={filesPanelSort}
          onSortChange={handleFilesSortChange}
        />
      )}
      {/* Mobile-only full-screen drawers for the rail tabs that have no
          desktop push panel of their own. `MobilePanelDrawer` is `md:hidden`,
          so these never collide with the desktop rail; they're opened from
          the session-menu FAB above. */}
      {conversationId && rootSessionId && (
        <MobilePanelDrawer
          open={subagentsPanelOpen}
          title="Agents"
          onClose={() => setSubagentsPanelOpen(false)}
          testId="subagents-panel-drawer"
        >
          <SubagentsPanel conversationId={conversationId} rootSessionId={rootSessionId} />
        </MobilePanelDrawer>
      )}
      {conversationId && (
        <MobilePanelDrawer
          open={shellsPanelOpen}
          title="Shells"
          onClose={() => setShellsPanelOpen(false)}
          testId="shells-panel-drawer"
        >
          <InlineTerminalsSection
            conversationId={conversationId}
            onExpand={openTerminalsPanel}
            // Mobile has no tab strip "+" menu, so the drawer carries
            // the "+ New shell" create row.
            showNewShell
          />
        </MobilePanelDrawer>
      )}
      {conversationId && showFilesPanel && (
        <MobilePanelDrawer
          open={githubPanelOpen}
          title="Pull Requests"
          onClose={() => setGithubPanelOpen(false)}
          testId="github-panel-drawer"
        >
          <RenderErrorBoundary name="pull requests" resetKey={conversationId}>
            <PullRequestPanel conversationId={conversationId} />
          </RenderErrorBoundary>
        </MobilePanelDrawer>
      )}
      {conversationId && mobileViewport && railTabsAvailable["content-search"] && (
        <MobilePanelDrawer
          open={contentSearchPanelOpen}
          title="Content search"
          onClose={() => setContentSearchPanelOpen(false)}
          testId="content-search-panel-drawer"
        >
          <ContentSearchPanel
            key={conversationId}
            conversationId={conversationId}
            onFileSelect={openContentSearchResult}
          />
        </MobilePanelDrawer>
      )}
      <div ref={setMobileFileTarget} className="md:hidden" />
    </>
  );
}
