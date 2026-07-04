// Manages the editor remount key (editorKey), dirty flag, and
// synchronisation between server-supplied content and the live editor.

import { useCallback, useEffect, useRef, useState } from "react";
import type { RefObject } from "react";

interface Options {
  content: string;
  path: string;
  /** True when the file query has settled (isSuccess). Used to defer the
   *  path-change remount until real content is available, avoiding an empty flash. */
  isSettled: boolean;
  onDirtyChange?: (isDirty: boolean) => void;
  /**
   * Ref to a function that updates the TipTap editor content in-place.
   * When non-null, used instead of incrementing editorKey for same-path
   * external updates so scroll position and cursor are preserved.
   * Falls back to editorKey increment when null (e.g. before the editor
   * has mounted, or when the hook is used outside a TipTap context).
   */
  setContentRef?: RefObject<((content: string) => void) | null>;
}

interface Result {
  /** Incremented each time the editor should remount. */
  editorKey: number;
  isDirty: boolean;
  setDirty: (value: boolean) => void;
  /**
   * True when a server content update arrived while the editor was dirty
   * (i.e. the user has unsaved edits that conflict with an external change).
   */
  hasExternalUpdate: boolean;
  /** Discard local edits and load the latest server content. */
  discardAndApplyExternal: () => void;
  /** Drop the stashed server content; the user's next save will win. */
  dismissExternalUpdate: () => void;
  /**
   * Record content the editor just persisted so its server echo is treated
   * as our own write, not an external edit — otherwise an auto-save echo that
   * lands after the user resumes typing falsely raises hasExternalUpdate.
   */
  markSaved: (content: string) => void;
  /**
   * Reconcile server content fetched outside the query (the pre-write
   * conflict check, since the query isn't refetched mid-turn). When it
   * differs from the last-known server state — and isn't our own save — it's
   * an external edit: on a dirty editor, stash it and raise hasExternalUpdate.
   *
   * :param serverContent: The freshly fetched raw file content.
   * :returns: True when a conflict was raised (caller must skip its write);
   *     False when already known and the write may proceed.
   */
  reconcileServerContent: (serverContent: string) => boolean;
}

export function useMarkdownEditorSync({
  content,
  path,
  isSettled,
  onDirtyChange,
  setContentRef,
}: Options): Result {
  const [editorKey, setEditorKey] = useState(0);
  const [isDirty, setIsDirty] = useState(false);
  const [hasExternalUpdate, setHasExternalUpdate] = useState(false);

  // isDirtyRef lets the content-change effect read the current dirty flag without
  // including isDirty in its dependency array (which would cause double-remounts).
  const isDirtyRef = useRef(isDirty);
  isDirtyRef.current = isDirty;

  // Stash for server content that arrived while the editor was dirty.
  // Applied the next time the editor becomes clean (e.g. after save).
  const pendingContentRef = useRef<string | null>(null);

  // Content this editor most recently saved, so its server echo is skipped
  // below instead of read as an external edit. Null until first save; reset on path change.
  const lastSavedRef = useRef<string | null>(null);

  const prevContentRef = useRef(content);
  const prevPathRef = useRef(path);

  // True when a path change is waiting for the new file's query to settle.
  const pendingRemountRef = useRef(false);

  const setDirty = useCallback(
    (value: boolean) => {
      setIsDirty(value);
      onDirtyChange?.(value);
    },
    [onDirtyChange],
  );

  // Path change → reset state and mark a pending remount.
  // Don't remount immediately: content may still be "" while the new file loads.
  useEffect(() => {
    if (prevPathRef.current === path) return;
    prevPathRef.current = path;
    prevContentRef.current = content; // suppress the content-change effect below
    pendingContentRef.current = null;
    lastSavedRef.current = null;
    setHasExternalUpdate(false);
    setDirty(false);
    pendingRemountRef.current = true;
  }, [path, content, setDirty]);

  // Execute the pending remount once the new file's query is settled.
  // Including `path` in deps ensures this fires on the same commit as the
  // path-change effect for cache hits (where isSettled stays true throughout).
  useEffect(() => {
    if (!isSettled || !pendingRemountRef.current) return;
    pendingRemountRef.current = false;
    // Sync prevContentRef so the content-change effect below doesn't see a
    // stale "previous" value and trigger a second remount after this one.
    prevContentRef.current = content;
    setEditorKey((k) => k + 1);
  }, [isSettled, path, content]);

  // When content arrives from the server (same path):
  // - Skip if a path-change remount is pending (that remount will use the settled content).
  // - Clean editor → update in-place via setContentRef (preserves scroll/cursor).
  //   Falls back to editorKey remount if the editor handle isn't available yet.
  // - Dirty editor (user is mid-edit) → stash and surface hasExternalUpdate so
  //   the UI can warn the user their save will overwrite the incoming change.
  useEffect(() => {
    if (prevContentRef.current === content) return;
    // Self-save echo: server reports exactly what we just saved. Skip it —
    // re-applying would reset the cursor and (if typing resumed) misfire a conflict.
    if (content === lastSavedRef.current) {
      prevContentRef.current = content;
      return;
    }
    prevContentRef.current = content;
    if (pendingRemountRef.current) return;
    if (isDirtyRef.current) {
      pendingContentRef.current = content;
      setHasExternalUpdate(true);
      return;
    }
    if (setContentRef?.current) {
      setContentRef.current(content);
    } else {
      setEditorKey((k) => k + 1);
    }
  }, [content, setContentRef]);

  // Once the editor becomes clean (after save or full undo), apply any stashed
  // server content so the editor catches up to what the server has.
  useEffect(() => {
    if (isDirty || pendingContentRef.current === null) return;
    const pending = pendingContentRef.current;
    pendingContentRef.current = null;
    setHasExternalUpdate(false);
    if (setContentRef?.current) {
      setContentRef.current(pending);
    } else {
      setEditorKey((k) => k + 1);
    }
  }, [isDirty, setContentRef]);

  // Discard local edits and load the stashed server content immediately.
  const discardAndApplyExternal = useCallback(() => {
    const pending = pendingContentRef.current;
    pendingContentRef.current = null;
    setHasExternalUpdate(false);
    setDirty(false);
    if (setContentRef?.current && pending !== null) {
      setContentRef.current(pending);
    } else {
      setEditorKey((k) => k + 1);
    }
  }, [setDirty, setContentRef]);

  // Drop the stash — the user's next save will overwrite the external change.
  const dismissExternalUpdate = useCallback(() => {
    pendingContentRef.current = null;
    setHasExternalUpdate(false);
  }, []);

  // Record the content just persisted so its server echo is ignored above.
  const markSaved = useCallback((saved: string) => {
    lastSavedRef.current = saved;
  }, []);

  // Imperative counterpart to the content-change effect, for content fetched
  // outside the query (the pre-write conflict check). See the Result doc.
  const reconcileServerContent = useCallback(
    (serverContent: string): boolean => {
      // Already known (last-seen server state or our own save) — nothing to do.
      if (serverContent === prevContentRef.current || serverContent === lastSavedRef.current) {
        return false;
      }
      // Adopt as the latest known server state — this also lets a later "Keep
      // mine" overwrite through, while a *newer* external edit re-raises.
      prevContentRef.current = serverContent;
      if (isDirtyRef.current) {
        pendingContentRef.current = serverContent;
        setHasExternalUpdate(true);
        return true;
      }
      // Clean editor: adopt the content (nothing to conflict with).
      if (setContentRef?.current) {
        setContentRef.current(serverContent);
      } else {
        setEditorKey((k) => k + 1);
      }
      return false;
    },
    [setContentRef],
  );

  return {
    editorKey,
    isDirty,
    setDirty,
    hasExternalUpdate,
    discardAndApplyExternal,
    dismissExternalUpdate,
    markSaved,
    reconcileServerContent,
  };
}
