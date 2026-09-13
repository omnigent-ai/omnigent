import { lazy, Suspense, useState } from "react";
import { PlusIcon, TerminalIcon, GlobeIcon } from "lucide-react";
import { toast } from "sonner";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  landingResourceTarget,
  landingContextClaimedElsewhere,
  readLandingWorkspaceState,
  useLandingWorkspaceState,
  writeLandingWorkspacePanel,
} from "@/lib/landingWorkspaceState";
import { supportsBrowser } from "@/lib/nativeBridge";
import { useWorkspaceChangedFiles } from "@/hooks/useWorkspaceChangedFiles";
import { terminalTabKey } from "@/hooks/useTerminals";
import { buildDraftTerminalAttachPath, type useDraftWorkspace } from "@/hooks/useDraftWorkspace";
import type { ChangedSort } from "./FlatFileList";
import type { RightRailTab } from "./railTabs";
import { WorkspacePanel } from "./WorkspacePanel";

const TerminalView = lazy(() =>
  import("@/components/blocks/TerminalView").then((m) => ({ default: m.TerminalView })),
);

export function LandingWorkspacePanel({
  width,
  handleProps,
  maximized,
  onToggleMaximized,
  draft,
}: {
  width: number;
  handleProps: React.HTMLAttributes<HTMLDivElement> & { tabIndex: number };
  maximized: boolean;
  onToggleMaximized: () => void;
  draft: ReturnType<typeof useDraftWorkspace>;
}) {
  const { selection, panel, browserNamespace, starting } = useLandingWorkspaceState();
  const target = landingResourceTarget(selection);
  const changed = useWorkspaceChangedFiles(target);
  const contextMatchesSelection =
    !landingContextClaimedElsewhere(draft.context?.id, browserNamespace) &&
    draft.context?.hostId === selection?.hostId &&
    (draft.context?.workspace === selection?.workspace ||
      draft.context?.workspaceAliases.includes(selection?.workspace ?? ""));
  const visibleDraft = contextMatchesSelection ? draft : { ...draft, terminals: [] };
  const [sort, setSort] = useState<ChangedSort>("recent");
  const [showHidden, setShowHidden] = useState(true);
  const [creating, setCreating] = useState(false);
  const openTerminal = (key: string) =>
    writeLandingWorkspacePanel({ selectedTerminalKey: key, selectedFilePath: null });
  const selectTab = (rightRailTab: RightRailTab) =>
    writeLandingWorkspacePanel({ rightRailTab, selectedFilePath: null, selectedTerminalKey: null });
  const openFile = (path: string) =>
    writeLandingWorkspacePanel({
      selectedFilePath: path,
      selectedTerminalKey: null,
      openFiles: [...new Set([...(panel.openFiles ?? []), path])],
    });
  const create = async () => {
    if (!selection?.available || !selection.hostId || creating || starting) return;
    setCreating(true);
    try {
      const context = await draft.ensureContext(selection.hostId, selection.workspace);
      const terminal = await draft.createTerminal(context);
      if (readLandingWorkspaceState().browserNamespace !== browserNamespace) return;
      openTerminal(terminalTabKey(terminal));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Couldn't open shell.");
    } finally {
      setCreating(false);
    }
  };
  return (
    <WorkspacePanel
      landing
      resourceCreationDisabled={starting}
      target={target}
      browserNamespace={browserNamespace}
      unavailableReason={selection?.reason}
      width={width}
      handleProps={handleProps}
      maximized={maximized}
      onToggleMaximized={onToggleMaximized}
      rightRailTab={panel.rightRailTab ?? "files"}
      onRightRailTabChange={selectTab}
      showFilesPanel
      showGithubTab={!!target}
      showBrowserTab={
        supportsBrowser() &&
        typeof (window as unknown as { omnigentDesktop?: { browserAdoptDraft?: unknown } })
          .omnigentDesktop?.browserAdoptDraft === "function"
      }
      changedCount={changed.data?.data.length ?? 0}
      subagentsWorking={0}
      agentCount={0}
      rootSessionId={null}
      selectedFilePath={panel.selectedFilePath ?? null}
      openFiles={panel.openFiles ?? []}
      openFileViewer={openFile}
      onCloseFile={(path) =>
        writeLandingWorkspacePanel({
          openFiles: panel.openFiles?.filter((file) => file !== path),
          ...(panel.selectedFilePath === path ? { selectedFilePath: null } : {}),
        })
      }
      onShowScopeView={() => writeLandingWorkspacePanel({ selectedFilePath: null })}
      onCommentsOpenChange={() => {}}
      openTerminalTab={openTerminal}
      openTerminals={visibleDraft.terminals.map(terminalTabKey)}
      selectedTerminalKey={panel.selectedTerminalKey ?? null}
      onCloseTerminal={(key) => {
        const terminal = visibleDraft.terminals.find((item) => terminalTabKey(item) === key);
        if (terminal)
          void draft.deleteTerminal(terminal.id).catch(() => toast.error("Couldn't close shell."));
      }}
      permissionLevel={3}
      filesPanelSort={sort}
      onSortChange={setSort}
      filesPanelShowHidden={showHidden}
      onShowHiddenChange={setShowHidden}
      draftTerminals={visibleDraft.terminals}
      draftTerminalView={
        <DraftTerminalSurface
          draft={visibleDraft}
          terminalKey={panel.selectedTerminalKey ?? null}
        />
      }
      renderDraftNewTabMenu={(onOpenBrowser) => (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              aria-label="Open new"
              className="flex size-6 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-muted"
            >
              <PlusIcon className="size-5" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
            <DropdownMenuItem
              disabled={!target || creating || starting}
              onSelect={() => void create()}
            >
              <TerminalIcon className="size-4" />
              {creating ? "Opening shell…" : "Shell (bash)"}
            </DropdownMenuItem>
            {onOpenBrowser && (
              <DropdownMenuItem disabled={starting} onSelect={onOpenBrowser}>
                <GlobeIcon className="size-4" />
                Browser
              </DropdownMenuItem>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    />
  );
}

export function DraftTerminalSurface({
  draft,
  terminalKey,
}: {
  draft: ReturnType<typeof useDraftWorkspace>;
  terminalKey: string | null;
}) {
  const terminal = draft.terminals.find((item) => terminalTabKey(item) === terminalKey);
  return terminal && draft.context ? (
    <Suspense fallback={<div>Connecting shell…</div>}>
      <TerminalView
        key={JSON.stringify([draft.context.hostId, draft.context.id, terminal.id])}
        terminalId={terminal.id}
        hostId={draft.context.hostId}
        attachPath={buildDraftTerminalAttachPath(
          draft.context.id,
          terminal.id,
          false,
          draft.context.hostId,
        )}
        focusOnConnect
      />
    </Suspense>
  ) : null;
}
