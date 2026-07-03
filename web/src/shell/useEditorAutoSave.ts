// Shared auto-save orchestration for the file-viewer editors (TipTap markdown +
// Monaco code). Owns everything that was identical between the two: the write
// mutation, the single-flight save with a mid-turn conflict check, the
// save-after-teardown guard, and the debounce/flush wiring (on edit, on unmount,
// on runner-reconnect). Each editor keeps its own *content access* — getContent
// / isEditorDirty / baselineRef — because that's intrinsically tied to the
// editor instance (TipTap's getMarkdown() vs Monaco's tracked buffer).

import { useCallback, useEffect, useRef } from "react";
import type { RefObject } from "react";
import { useWriteFileContent } from "@/hooks/useWriteFileContent";
import { fetchFileContent } from "@/hooks/useFileContent";
import { useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { useChatStore } from "@/store/chatStore";
import { useAutoSave } from "./useAutoSave";

// Debounce between the last edit and an auto-save — long enough to coalesce a
// burst of typing, short enough to keep the dirty window brief. Shared so both
// editor surfaces behave identically.
const AUTOSAVE_DELAY_MS = 1000;

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
  reconcileServerContent: (serverContent: string) => boolean;
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
  /** True when the runner is offline — surfaced in the editor's status UI. */
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
  reconcileServerContent,
  dismissExternalUpdate,
  baselineRef,
  getContent,
  isEditorDirty,
}: UseEditorAutoSaveOptions): UseEditorAutoSaveResult {
  const writeFile = useWriteFileContent(conversationId);
  const writeFileRef = useRef(writeFile);
  writeFileRef.current = writeFile;
  const runnerOnline = useSessionRunnerOnline(conversationId);
  const saveDisabled = runnerOnline === false;

  // The only concurrent writer is the agent during its turn, so the pre-write
  // conflict check runs only while the session is active — when idle we write
  // directly (no extra GET, zero added latency).
  const focusedId = useChatStore((s) => s.conversationId);
  const sessionStatus = useChatStore((s) => s.sessionStatus);
  const sessionActive =
    conversationId === focusedId && (sessionStatus === "running" || sessionStatus === "waiting");
  const sessionActiveRef = useRef(sessionActive);
  sessionActiveRef.current = sessionActive;

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

  const handleSave = useCallback(
    async (value: string) => {
      if (value === baselineRef.current) {
        setDirty(false);
        return;
      }
      writeFileRef.current.reset();
      try {
        // Pre-write conflict check: mid-turn the file query isn't refetched, so
        // GET the live file first; if it changed under us, raise the conflict
        // and skip the write instead of clobbering the agent's edit.
        if (sessionActiveRef.current) {
          try {
            const fresh = await fetchFileContent(conversationId, path);
            // Torn down during the GET → don't touch the outer sync hook (it now
            // tracks a different file); fall through and let the write land.
            if (
              mountedRef.current &&
              fresh.encoding === "utf-8" &&
              reconcileServerContent(fresh.content)
            ) {
              return;
            }
          } catch {
            // Best-effort: a failed conflict-check GET must not block saving.
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
        // Force clean after a successful write. Do NOT re-derive dirty from a
        // live content-vs-baseline compare: an editor whose serialisation isn't
        // byte-stable (TipTap's markdown round-trip) can spuriously report dirty
        // and leave the editor stuck "Unsaved" / looping.
        setDirty(false);
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
      reconcileServerContent,
      baselineRef,
    ],
  );

  // Auto-save is suppressed when read-only, offline, or an external-edit
  // conflict is unresolved (user must pick Keep mine / Load latest first).
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
  // again (runner reconnects, or the conflict is resolved via "Keep mine").
  const prevAutoSaveEnabledRef = useRef(autoSaveEnabled);
  useEffect(() => {
    const wasEnabled = prevAutoSaveEnabledRef.current;
    prevAutoSaveEnabledRef.current = autoSaveEnabled;
    if (!wasEnabled && autoSaveEnabled && isDirty) autoSave.flush();
  }, [autoSaveEnabled, isDirty, autoSave]);

  return { autoSave, saveDisabled, writeFile };
}
