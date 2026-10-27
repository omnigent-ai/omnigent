// Shared auto-save orchestration for the file-viewer editors (TipTap markdown +
// Monaco code). Owns everything that was identical between the two: the write
// mutation, the single-flight save with a pre-write conflict check, the
// save-after-teardown guard, and the debounce/flush wiring (on edit, on unmount,
// on reconnect). Each editor keeps its own *content access* — getContent
// / isEditorDirty / baselineRef — because that's intrinsically tied to the
// editor instance (TipTap's getMarkdown() vs Monaco's tracked buffer).

import { useCallback, useEffect, useRef } from "react";
import type { RefObject } from "react";
import { useWriteFileContent } from "@/hooks/useWriteFileContent";
import { fetchFileContent } from "@/hooks/useFileContent";
import { useWorkspaceServeable } from "@/hooks/useWorkspaceChangedFiles";
import { useAutoSave } from "./useAutoSave";
import type { FinishServerCheck } from "./useMarkdownEditorSync";

// Debounce between the last edit and an auto-save — long enough to coalesce a
// burst of typing, short enough to keep the dirty window brief. Shared so both
// editor surfaces behave identically.
const AUTOSAVE_DELAY_MS = 1000;

// A stalled pre-write conflict check gives up after this long, and the save
// proceeds as if the check had failed.
export const CONFLICT_CHECK_TIMEOUT_MS = 10_000;

interface UseEditorAutoSaveOptions {
  conversationId: string;
  path: string;
  /** Editing permitted (edit permission and not truncated). Gates auto-save. */
  canEdit: boolean;
  // ── From useMarkdownEditorSync (the shared sync / dirty / conflict hook) ──
  /** React dirty flag; drives the flush-on-reconnect effect. */
  isDirty: boolean;
  setDirty: (dirty: boolean) => void;
  /** True while an unresolved external-edit conflict suppresses auto-save. */
  hasExternalUpdate: boolean;
  markSaved: (content: string) => void;
  beginServerCheck: () => FinishServerCheck;
  dismissExternalUpdate: () => void;
  // ── Editor-specific content access ──
  /**
   * Last-saved baseline. Compared against on save (to skip a no-op write) and
   * advanced to the saved content on success. The editor also writes it from
   * its own mount / external-update paths; null means "not yet baselined".
   */
  baselineRef: RefObject<string | null>;
  /** Live buffer content, read lazily at save time. */
  getContent: () => string;
  /** Live dirty check (buffer vs baseline) without re-reading React state. */
  isEditorDirty: () => boolean;
}

interface UseEditorAutoSaveResult {
  /** Stable schedule / flush / cancel — safe as an effect dependency. */
  autoSave: ReturnType<typeof useAutoSave>;
  /** True when the workspace is unreachable — surfaced in the editor's status UI. */
  saveDisabled: boolean;
  /** The write mutation, for save-status UI (isPending / isError / isSuccess). */
  writeFile: ReturnType<typeof useWriteFileContent>;
}

/**
 * Wire an editor's live content to debounced, single-flight auto-save with
 * external-edit conflict detection and a teardown guard.
 *
 * @param options See {@link UseEditorAutoSaveOptions}.
 * @returns The auto-save controls plus the offline flag and write mutation for
 *   the caller's status UI.
 */
export function useEditorAutoSave({
  conversationId,
  path,
  canEdit,
  isDirty,
  setDirty,
  hasExternalUpdate,
  markSaved,
  beginServerCheck,
  dismissExternalUpdate,
  baselineRef,
  getContent,
  isEditorDirty,
}: UseEditorAutoSaveOptions): UseEditorAutoSaveResult {
  const writeFile = useWriteFileContent(conversationId);
  const writeFileRef = useRef(writeFile);
  writeFileRef.current = writeFile;
  // A reachable host lets the save endpoint reconnect a sleeping runner.
  // Only buffer edits when neither the runner nor its host is reachable.
  const saveDisabled = useWorkspaceServeable(conversationId) === false;

  // False once this editor instance unmounts (path switch / key remount / panel
  // close). A save in flight can resolve after teardown: the network write still
  // lands, but the post-write mutations on the *persistent* outer sync hook are
  // gated on this so a late markSaved()/setDirty(false) can't leak this file's
  // saved-state into the file the hook now tracks. Set in setup (not just
  // cleanup) so StrictMode's mount→unmount→remount restores it to true.
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // handleSave restarts itself when its conflict check went stale.
  const handleSaveRef = useRef<(value: string) => Promise<void>>(async () => {});
  const handleSave = useCallback(
    async (value: string) => {
      if (value === baselineRef.current) {
        setDirty(false);
        return;
      }
      writeFileRef.current.reset();
      try {
        // GET the live file first: if another writer changed it since the last
        // refresh, raise the conflict instead of clobbering their edit. A write
        // landing between this GET and the PUT can still be lost (last writer wins).
        const finishCheck = beginServerCheck();
        const checkTimeout = new AbortController();
        const timer = window.setTimeout(() => checkTimeout.abort(), CONFLICT_CHECK_TIMEOUT_MS);
        let serverContent: string | null = null;
        try {
          const fresh = await fetchFileContent(conversationId, path, checkTimeout.signal);
          if (fresh.encoding === "utf-8") serverContent = fresh.content;
        } catch {
          // Best-effort: a failed or timed-out conflict-check GET must not block saving.
        } finally {
          window.clearTimeout(timer);
        }
        // Torn down during the GET → don't touch the outer sync hook (it now
        // tracks a different file); fall through and let the write land.
        if (mountedRef.current) {
          const outcome = finishCheck(serverContent);
          if (outcome === "conflict") return;
          // A refresh landed during the GET, so its response and `value` may be
          // older than what's shown: start over from the current buffer.
          if (outcome === "stale") return handleSaveRef.current(getContent());
          // Clean again after the GET (undone, or a refresh applied newer server
          // content): writing the captured value would overwrite what's shown.
          if (!isEditorDirty()) {
            setDirty(false);
            return;
          }
        }
        await writeFileRef.current.mutateAsync({ path, content: value });
        // The write lands regardless, but if this editor was torn down mid-write
        // the outer sync hook now tracks another file — skip the mutations below.
        if (!mountedRef.current) return;
        baselineRef.current = value;
        // Mark so the server echo is recognised as our own write, not an
        // external edit (which would falsely raise hasExternalUpdate).
        markSaved(value);
        // Edits typed during the write stay unsaved, so an external update must
        // raise a conflict rather than replace them. isEditorDirty compares the
        // buffer or document snapshot just saved, never re-serialised Markdown.
        setDirty(isEditorDirty());
        // The user's save wins; drop any stashed external content so the
        // "became clean" sync effect doesn't overwrite the editor with it.
        dismissExternalUpdate();
      } catch {
        // Surfaced via writeFile.isError in the editor's status UI.
      }
    },
    [
      conversationId,
      path,
      setDirty,
      dismissExternalUpdate,
      markSaved,
      beginServerCheck,
      baselineRef,
      getContent,
      isEditorDirty,
    ],
  );
  handleSaveRef.current = handleSave;

  // Auto-save is suppressed when read-only, the workspace is unreachable, or an
  // external-edit conflict is unresolved (user must pick Keep mine / Load latest
  // first).
  const autoSaveEnabled = canEdit && !saveDisabled && !hasExternalUpdate;

  const autoSave = useAutoSave({
    delayMs: AUTOSAVE_DELAY_MS,
    enabled: autoSaveEnabled,
    getContent,
    isDirty: isEditorDirty,
    save: handleSave,
  });

  // Flush a pending debounce on unmount (file switch / panel close) so an edit
  // made within the debounce window isn't lost; the write lands via React Query.
  useEffect(
    () => () => {
      autoSave.flush();
    },
    [autoSave],
  );

  // Flush edits accumulated while auto-save was blocked, once it's eligible
  // again (the workspace is reachable again, or the conflict is resolved via
  // "Keep mine").
  const prevAutoSaveEnabledRef = useRef(autoSaveEnabled);
  useEffect(() => {
    const wasEnabled = prevAutoSaveEnabledRef.current;
    prevAutoSaveEnabledRef.current = autoSaveEnabled;
    if (!wasEnabled && autoSaveEnabled && isDirty) autoSave.flush();
  }, [autoSaveEnabled, isDirty, autoSave]);

  return { autoSave, saveDisabled, writeFile };
}
