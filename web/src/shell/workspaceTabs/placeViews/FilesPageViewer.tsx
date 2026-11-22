// A file in a workspace Files tab: text in the CodeMirror editor, which saves as it goes,
// and Markdown (rich preview and edit), images, PDFs, 3D models and binaries in the app's viewer.

import { FileDiffIcon, XIcon } from "lucide-react";
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
import { useComparisonFileDiff } from "@/hooks/useComparisonFileDiff";
import { useFileContent } from "@/hooks/useFileContent";
import { fileContentText } from "@/lib/workspaceSidebarSource";
import { cn } from "@/lib/utils";
import { CodeMirrorFileEditor } from "@/shell/codeMirror/CodeMirrorFileEditor";
import {
  detectLang,
  isBinaryPath,
  isImageFile,
  isModelFile,
  isPdfFile,
} from "@/shell/codeViewerHelpers";
import { FileViewer } from "@/shell/FileViewer";
import type { FilePosition } from "@/shell/FileViewerContext";

import { WORKSPACE_SIDEBAR_LAYOUT, WORKSPACE_TOOLBAR_CLASS } from "../WorkspaceSidebarLayout";

/** Asks the user about unsaved edits before the tab moves on: leave, or keep editing. */
export type FilesPageLeaveGuard = () => Promise<"leave" | "cancel">;

export interface FilesPageViewerProps {
  conversationId: string;
  path: string;
  /** A cited line to reveal; a new object reveals it again. */
  position?: FilePosition;
  /** The tab is selected and the sidebar is open, so the file stays current. */
  active: boolean;
  /** Shown first in the toolbar, such as the tree's toggle. */
  toolbarLeading?: ReactNode;
  onClose: () => void;
  /** Shows the file's changes, in the Changes review. */
  onShowChanges?: () => void;
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
  toolbarLeading,
  onClose,
  onShowChanges,
  registerLeaveGuard,
  permissionLevel,
  commentId,
}: FilesPageViewerProps) {
  const file = useFileContent(conversationId, path, { visible: active });
  const contentType = file.data?.content_type;
  // Markdown, media and binaries show in the app's viewer, under the same toolbar as text.
  const rich =
    file.data != null && (commentId !== undefined || showsInFileViewer(path, contentType));
  const viewerProps: AppViewerProps = {
    linkedCommentId: commentId,
    conversationId,
    path,
    position,
    open: active,
    frameless: true,
    onClose,
    registerLeaveGuard,
    permissionLevel,
  };
  // Another linked comment opens its thread afresh.
  const appViewer = <ComparisonFileViewer key={commentId} {...viewerProps} />;
  const text = file.data && !rich ? fileContentText(file.data) : null;
  return (
    <div className={cn("flex min-h-0 flex-col bg-background", WORKSPACE_SIDEBAR_LAYOUT.page)}>
      <div className={cn(WORKSPACE_TOOLBAR_CLASS, WORKSPACE_SIDEBAR_LAYOUT.toolbar)}>
        {toolbarLeading}
        {/* The app's viewer names the file in its own header. */}
        <span
          className="min-w-0 flex-1 truncate font-mono text-ui text-muted-foreground"
          title={rich ? undefined : path}
        >
          {rich ? null : path}
        </span>
        {onShowChanges && (
          <Button type="button" variant="ghost" size="sm" onClick={onShowChanges}>
            <FileDiffIcon aria-hidden="true" />
            Changes
          </Button>
        )}
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Close file"
          onClick={onClose}
        >
          <XIcon aria-hidden="true" />
        </Button>
      </div>
      <div className={cn("flex min-h-0 flex-1 flex-col", WORKSPACE_SIDEBAR_LAYOUT.content)}>
        {/* A failed refresh keeps the last content, so an open editor and its edits stay. */}
        {file.isError && !file.data ? (
          <UnreadableFile viewer={viewerProps} message={file.error.message} />
        ) : !file.data ? (
          <div className="flex flex-1 items-center justify-center">
            <Spinner aria-label={`Loading ${path}`} />
          </div>
        ) : text === null ? (
          // A rich file, or bytes that aren't text under a name that doesn't say so.
          appViewer
        ) : (
          <TextFile
            conversationId={conversationId}
            path={path}
            content={text}
            truncated={file.data.truncated === true}
            settled={!file.isFetching}
            position={position}
            registerLeaveGuard={registerLeaveGuard}
          />
        )}
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
function UnreadableFile({ viewer, message }: { viewer: AppViewerProps; message: string }) {
  const comparisonDiff = useComparisonFileDiff(viewer.conversationId, viewer.path);
  if (comparisonDiff.base === "comparison" && comparisonDiff.deleted) {
    return <FileViewer {...viewer} comparisonDiff={comparisonDiff} />;
  }
  return (
    <p role="alert" className="p-3 text-ui text-destructive">
      Couldn’t open {viewer.path}: {message}
    </p>
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
}: {
  conversationId: string;
  path: string;
  content: string;
  truncated: boolean;
  settled: boolean;
  position?: FilePosition;
  registerLeaveGuard?: (guard: FilesPageLeaveGuard) => () => void;
}) {
  const [searchOpen, setSearchOpen] = useState(false);
  const [dirty, setDirty] = useState(false);
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
        searchOpen={searchOpen}
        onSearchOpened={() => setSearchOpen(true)}
        onSearchHandled={() => setSearchOpen(false)}
        onDirtyChange={setDirty}
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
