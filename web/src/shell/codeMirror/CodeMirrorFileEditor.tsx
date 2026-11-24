// The CodeMirror viewer as a file editor. It saves the way the Monaco editor
// does, through the same sync and auto-save hooks: edits save after a pause, on
// ⌘S, or when focus leaves, and a change on disk meanwhile asks before replacing them.

import type { Text } from "@codemirror/state";
import { useCallback, useEffect, useMemo, useRef } from "react";
import { useCanEdit } from "@/hooks/usePermissions";
import type { SaveStatus } from "../codeViewerHelpers";
import { EditorSyncBanner } from "../EditorSyncBanner";
import { useEditorAutoSave } from "../useEditorAutoSave";
import { useMarkdownEditorSync } from "../useMarkdownEditorSync";
import { useSaveStatusReport } from "../useSaveStatusReport";
import type { CodeMirrorEditorHandle } from "./codeMirrorEditing";
import { CodeMirrorFileViewer, type CodeMirrorFileViewerProps } from "./CodeMirrorFileViewer";
import { documentOf, preferredLineBreak, savedTextOf } from "./rawOffsets";

export interface CodeMirrorFileEditorProps extends Omit<
  CodeMirrorFileViewerProps,
  "editing" | "banner"
> {
  /** True once the file query has settled, so a file switch remounts with real content. */
  isSettled: boolean;
  /** Reports unsaved edits up to FileViewer, which guards navigation. */
  onDirtyChange?: (isDirty: boolean) => void;
  /** Reports the auto-save lifecycle to the toolbar's status chip. */
  onSaveStatusChange?: (status: SaveStatus) => void;
}

export function CodeMirrorFileEditor({
  content,
  path,
  isSettled,
  onDirtyChange,
  ...props
}: CodeMirrorFileEditorProps) {
  const setContentRef = useRef<((content: string) => void) | null>(null);
  const sync = useMarkdownEditorSync({ content, path, isSettled, onDirtyChange, setContentRef });
  // A file switch remounts the editor; report it clean so nothing keeps waiting on its edits.
  const onDirtyChangeRef = useRef(onDirtyChange);
  onDirtyChangeRef.current = onDirtyChange;
  useEffect(() => () => onDirtyChangeRef.current?.(false), []);
  return (
    <CodeMirrorFileEditorSession
      key={sync.editorKey}
      {...props}
      content={content}
      path={path}
      sync={sync}
      setContentRef={setContentRef}
    />
  );
}

interface SessionProps extends Omit<CodeMirrorFileEditorProps, "isSettled" | "onDirtyChange"> {
  sync: ReturnType<typeof useMarkdownEditorSync>;
  setContentRef: React.RefObject<((content: string) => void) | null>;
}

export const TRUNCATED_READ_ONLY_REASON =
  "Only part of this large file is loaded, so it can't be edited here.";
export const NO_EDIT_ACCESS_REASON = "You can view this session, but not edit its files.";

/** One editing session over one copy of the file; remounted when the file is switched. */
function CodeMirrorFileEditorSession({
  content,
  conversationId,
  path,
  truncated,
  sync,
  setContentRef,
  onSaveStatusChange,
  commentOptions,
  ...props
}: SessionProps) {
  const { isDirty, setDirty, hasExternalUpdate, markSaved, beginServerCheck } = sync;
  // A truncated buffer is never editable: saving it would cut off the rest of the file.
  const hasEditAccess = useCanEdit(conversationId);
  const canEdit = hasEditAccess && !truncated;
  const readOnlyReason = truncated
    ? TRUNCATED_READ_ONLY_REASON
    : hasEditAccess
      ? undefined
      : NO_EDIT_ACCESS_REASON;
  const editorRef = useRef<CodeMirrorEditorHandle | null>(null);
  // Saves keep the file's usual line break, so an unedited CRLF file writes back unchanged.
  const lineBreak = useMemo(() => preferredLineBreak(content), [content]);
  // The saved text, and its lines: edits are dirty when the document differs from them.
  const baselineRef = useRef<string | null>(content);
  const baselineDocumentRef = useRef<Text>(documentOf(content));
  const documentRef = useRef<Text>(baselineDocumentRef.current);

  const isEditorDirty = useCallback(() => !documentRef.current.eq(baselineDocumentRef.current), []);
  const getContent = useCallback(() => savedTextOf(documentRef.current, lineBreak), [lineBreak]);
  const markSavedText = useCallback(
    (saved: string) => {
      baselineDocumentRef.current = documentOf(saved);
      editorRef.current?.markSaved(saved);
      markSaved(saved);
    },
    [markSaved],
  );
  const { autoSave, saveDisabled, writeFile } = useEditorAutoSave({
    conversationId,
    path,
    canEdit,
    isDirty,
    setDirty,
    hasExternalUpdate,
    markSaved: markSavedText,
    beginServerCheck,
    dismissExternalUpdate: sync.dismissExternalUpdate,
    baselineRef,
    getContent,
    isEditorDirty,
  });
  const autoSaveRef = useRef(autoSave);
  autoSaveRef.current = autoSave;

  // A newer copy of the file, from a refresh or "Load latest", replaces a clean buffer in place.
  useEffect(() => {
    setContentRef.current = (next: string) => {
      baselineRef.current = next;
      baselineDocumentRef.current = documentOf(next);
      documentRef.current = baselineDocumentRef.current;
      editorRef.current?.loadContent(next);
      setDirty(false);
    };
    return () => {
      setContentRef.current = null;
    };
  }, [setContentRef, setDirty]);

  const { isPending: writePending, isError: writeError, isSuccess: writeSuccess } = writeFile;
  useSaveStatusReport(
    { writePending, writeError, writeSuccess, saveDisabled, isDirty },
    onSaveStatusChange,
  );

  const editing = useMemo(
    () => ({
      editable: canEdit,
      readOnlyReason,
      editorRef,
      onChange: (doc: Text) => {
        documentRef.current = doc;
        const dirty = isEditorDirty();
        setDirty(dirty);
        if (dirty) autoSaveRef.current.schedule();
      },
      onSave: () => autoSaveRef.current.flush(),
      onBlur: () => autoSaveRef.current.flush(),
    }),
    [canEdit, readOnlyReason, isEditorDirty, setDirty],
  );

  return (
    <CodeMirrorFileViewer
      {...props}
      content={content}
      conversationId={conversationId}
      path={path}
      truncated={truncated}
      editing={editing}
      // Offsets only match the saved file while the buffer is clean.
      commentOptions={
        commentOptions && {
          ...commentOptions,
          canComment: commentOptions.canComment && !isDirty,
        }
      }
      banner={
        canEdit &&
        isDirty && (
          <EditorSyncBanner
            hasExternalUpdate={hasExternalUpdate}
            onKeepMine={sync.dismissExternalUpdate}
            onLoadLatest={sync.discardAndApplyExternal}
          />
        )
      }
    />
  );
}
