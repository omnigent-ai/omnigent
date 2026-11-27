// A file in a workspace Files tab: text in the CodeMirror editor, which saves as it goes,
// and Markdown (rich preview and edit), images, PDFs, 3D models and binaries in the app's viewer.

import { FileDiffIcon } from "lucide-react";
import {
  type ComponentProps,
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { useComparisonFileDiff } from "@/hooks/useComparisonFileDiff";
import { useFileContent } from "@/hooks/useFileContent";
import { useStickyScroll } from "@/lib/fileViewPreferences";
import { fileContentText } from "@/lib/workspaceSidebarSource";
import { isOwnerLevel } from "@/lib/permissionsApi";
import { cn } from "@/lib/utils";
import { CodeMirrorFileEditor } from "@/shell/codeMirror/CodeMirrorFileEditor";
import {
  type SaveStatus,
  detectLang,
  isBinaryPath,
  isImageFile,
  isModelFile,
  isPdfFile,
} from "@/shell/codeViewerHelpers";
import { workspaceFolderName } from "@/shell/breadcrumbPaths";
import { FileBreadcrumbs } from "@/shell/FileBreadcrumbs";
import { FileViewer } from "@/shell/FileViewer";
import { type FilePosition, useWorkspacePaths } from "@/shell/FileViewerContext";
import { SaveStatusChip } from "@/shell/SaveStatusChip";

import { WORKSPACE_SIDEBAR_LAYOUT, WORKSPACE_TOOLBAR_CLASS } from "../WorkspaceSidebarLayout";
import { FileActionsMenu } from "./FileActionsMenu";

export const SHOW_CHANGES_LABEL = "Show changes";

/** Asks the user about unsaved edits before the tab moves on: leave, or keep editing. */
export type FilesPageLeaveGuard = () => Promise<"leave" | "cancel">;

export interface FilesPageViewerProps {
  conversationId: string;
  path: string;
  /** A cited line to reveal; a new object reveals it again. */
  position?: FilePosition;
  /** The tab is selected and the sidebar is open, so the file stays current. */
  active: boolean;
  /** Shown last in the toolbar, over the tree it toggles. */
  toolbarTrailing?: ReactNode;
  onClose: () => void;
  /** Shows the file's changes, in the Changes review. */
  onShowChanges?: () => void;
  /** Opens a file picked from the path's breadcrumbs; without it they only show the path. */
  onOpenFile?: (path: string) => void;
  /** Registers the question the tab asks before it leaves unsaved edits; returns the unregister. */
  registerLeaveGuard?: (guard: FilesPageLeaveGuard) => () => void;
  /** The session's permission level, which limits the app's viewer as it does elsewhere. */
  permissionLevel?: number | null;
  /** A comment whose thread a link opens; the app's viewer shows it, since the editor has no threads. */
  commentId?: string;
}

/** Whether the app's own viewer shows `path` rather than the text editor. */
export function showsInFileViewer(path: string, contentType: string | null | undefined): boolean {
  return (
    detectLang(path) === "markdown" ||
    isImageFile(path, contentType) ||
    isPdfFile(path, contentType) ||
    isModelFile(path, contentType) ||
    isBinaryPath(path)
  );
}

export function FilesPageViewer({
  conversationId,
  path,
  position,
  active,
  toolbarTrailing,
  onClose,
  onShowChanges,
  onOpenFile,
  registerLeaveGuard,
  permissionLevel,
  commentId,
}: FilesPageViewerProps) {
  const { root: workspaceRoot } = useWorkspacePaths();
  // The path as breadcrumbs, each folder listing the files beside it.
  const breadcrumbs = (
    <TooltipProvider>
      <FileBreadcrumbs
        conversationId={conversationId}
        path={path}
        workspaceName={workspaceFolderName(workspaceRoot)}
        onOpenFile={onOpenFile}
        canBrowseOutside={isOwnerLevel(permissionLevel ?? null)}
      />
    </TooltipProvider>
  );
  const file = useFileContent(conversationId, path, { visible: active });
  const contentType = file.data?.content_type;
  // Markdown, media and binaries show in the app's viewer, which then draws the toolbar row.
  const rich =
    file.data != null && (commentId !== undefined || showsInFileViewer(path, contentType));
  const viewerProps: AppViewerProps = {
    linkedCommentId: commentId,
    conversationId,
    path,
    position,
    open: active,
    frameless: true,
    besideSidebar: true,
    toolbarTrailing,
    onShowChanges,
    onClose,
    registerLeaveGuard,
    permissionLevel,
    pathHeader: breadcrumbs,
  };
  const text = file.data && !rich ? fileContentText(file.data) : null;
  const [searchOpen, setSearchOpen] = useState(false);
  // Each Find in file bumps it, so a repeat refocuses a find bar that is already open.
  const [searchFocusNonce, setSearchFocusNonce] = useState(0);
  const [saveStatus, setSaveStatus] = useState<SaveStatus>("idle");
  // A rich file, or bytes that aren't text under a name that doesn't say so, has one toolbar
  // row: the app viewer's. Another linked comment opens its thread afresh.
  if (file.data && text === null) return <ComparisonFileViewer key={commentId} {...viewerProps} />;
  // A failed refresh keeps the last content, so an open editor and its edits stay.
  if (file.isError && !file.data) {
    return (
      <UnreadableFile
        viewer={viewerProps}
        message={file.error.message}
        breadcrumbs={breadcrumbs}
        toggle={toolbarTrailing}
      />
    );
  }
  return (
    <FilesPage
      breadcrumbs={breadcrumbs}
      toggle={toolbarTrailing}
      trailing={
        <>
          {text !== null ? <SaveStatusChip status={saveStatus} /> : null}
          {onShowChanges && (
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    aria-label={SHOW_CHANGES_LABEL}
                    onClick={onShowChanges}
                  >
                    <FileDiffIcon aria-hidden="true" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent side="bottom">{SHOW_CHANGES_LABEL}</TooltipContent>
              </Tooltip>
            </TooltipProvider>
          )}
          {text !== null ? (
            <TooltipProvider>
              <FileActionsMenu
                conversationId={conversationId}
                path={path}
                onFind={() => {
                  setSearchOpen(true);
                  setSearchFocusNonce((nonce) => nonce + 1);
                }}
              />
            </TooltipProvider>
          ) : null}
        </>
      }
    >
      {!file.data || text === null ? (
        <div className="flex flex-1 items-center justify-center">
          <Spinner aria-label={`Loading ${path}`} />
        </div>
      ) : (
        <TextFile
          conversationId={conversationId}
          path={path}
          content={text}
          truncated={file.data.truncated === true}
          settled={!file.isFetching}
          position={position}
          registerLeaveGuard={registerLeaveGuard}
          searchOpen={searchOpen}
          searchFocusNonce={searchFocusNonce}
          onSearchOpenChange={setSearchOpen}
          onSaveStatusChange={setSaveStatus}
        />
      )}
    </FilesPage>
  );
}

/** The page's toolbar row, spanning the tree, over the file beside it. */
function FilesPage({
  breadcrumbs,
  trailing,
  toggle,
  children,
}: {
  breadcrumbs: ReactNode;
  /** The file's controls, at the right. */
  trailing: ReactNode;
  /** Last in the row, over the tree it toggles. */
  toggle: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className={cn("flex min-h-0 flex-col bg-background", WORKSPACE_SIDEBAR_LAYOUT.page)}>
      <div className={cn(WORKSPACE_TOOLBAR_CLASS, WORKSPACE_SIDEBAR_LAYOUT.toolbar)}>
        <div className="flex min-w-0 flex-1">{breadcrumbs}</div>
        {trailing}
        {toggle}
      </div>
      <div className={cn("flex min-h-0 flex-1 flex-col", WORKSPACE_SIDEBAR_LAYOUT.content)}>
        {children}
      </div>
    </div>
  );
}

type AppViewerProps = Omit<ComponentProps<typeof FileViewer>, "comparisonDiff">;

/** The app's viewer, whose diff is the one the Changes review shows under the session's comparison. */
function ComparisonFileViewer(props: AppViewerProps) {
  const comparisonDiff = useComparisonFileDiff(props.conversationId, props.path);
  return <FileViewer {...props} comparisonDiff={comparisonDiff} />;
}

/** A file that couldn't be read: the comparison's diff when it deleted the file, else why. */
function UnreadableFile({
  viewer,
  message,
  breadcrumbs,
  toggle,
}: {
  viewer: AppViewerProps;
  message: string;
  /** The page's own toolbar row, which the app's viewer would otherwise draw. */
  breadcrumbs: ReactNode;
  toggle: ReactNode;
}) {
  const comparisonDiff = useComparisonFileDiff(viewer.conversationId, viewer.path);
  if (comparisonDiff.base === "comparison" && comparisonDiff.deleted) {
    return <FileViewer {...viewer} comparisonDiff={comparisonDiff} />;
  }
  return (
    <FilesPage breadcrumbs={breadcrumbs} trailing={null} toggle={toggle}>
      <p role="alert" className="p-3 text-ui text-destructive">
        Couldn’t open {viewer.path}: {message}
      </p>
    </FilesPage>
  );
}

function TextFile({
  conversationId,
  path,
  content,
  truncated,
  settled,
  position,
  registerLeaveGuard,
  searchOpen,
  searchFocusNonce,
  onSearchOpenChange,
  onSaveStatusChange,
}: {
  conversationId: string;
  path: string;
  content: string;
  truncated: boolean;
  settled: boolean;
  position?: FilePosition;
  registerLeaveGuard?: (guard: FilesPageLeaveGuard) => () => void;
  /** The editor's find bar is open, as from the toolbar's Find in file. */
  searchOpen: boolean;
  searchFocusNonce: number;
  onSearchOpenChange: (open: boolean) => void;
  onSaveStatusChange: (status: SaveStatus) => void;
}) {
  const [dirty, setDirty] = useState(false);
  const stickyScroll = useStickyScroll();
  const leave = useUnsavedChangesQuestion();
  useEffect(() => {
    if (!dirty || !registerLeaveGuard) return undefined;
    return registerLeaveGuard(leave.ask);
  }, [dirty, registerLeaveGuard, leave.ask]);
  return (
    <>
      <CodeMirrorFileEditor
        conversationId={conversationId}
        path={path}
        content={content}
        truncated={truncated}
        isSettled={settled}
        position={position}
        wrapLines={false}
        stickyScroll={stickyScroll}
        searchOpen={searchOpen}
        searchFocusNonce={searchFocusNonce}
        onSearchOpened={() => onSearchOpenChange(true)}
        onSearchHandled={() => onSearchOpenChange(false)}
        onDirtyChange={setDirty}
        onSaveStatusChange={onSaveStatusChange}
      />
      {leave.dialog}
    </>
  );
}

/** The Unsaved changes question, as a promise the tab's leave guard awaits. */
function useUnsavedChangesQuestion() {
  const [open, setOpen] = useState(false);
  const answer = useRef<((choice: "leave" | "cancel") => void) | null>(null);
  const settle = (choice: "leave" | "cancel") => {
    answer.current?.(choice);
    answer.current = null;
    setOpen(false);
  };
  const ask = useCallback(
    () =>
      new Promise<"leave" | "cancel">((resolve) => {
        answer.current?.("cancel");
        answer.current = resolve;
        setOpen(true);
      }),
    [],
  );
  // An unmount mid-question keeps the edits: nothing is left waiting on it.
  useEffect(() => () => answer.current?.("cancel"), []);
  const dialog = (
    <Dialog open={open} onOpenChange={(isOpen) => !isOpen && settle("cancel")}>
      <DialogContent showCloseButton={false}>
        <DialogHeader>
          <DialogTitle>Unsaved changes</DialogTitle>
          <DialogDescription>
            Your edits will be lost if you leave without saving.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={() => settle("cancel")}>
            Keep editing
          </Button>
          <Button variant="destructive" onClick={() => settle("leave")}>
            Discard changes
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
  return { ask, dialog };
}
