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

/**
 * Finish a pre-write conflict check.
 *
 * :param serverContent: The fetched raw file content, or null when the GET
 *     failed or returned a binary file.
 * :returns: ``"conflict"`` when an external edit is pending (raised now or by a
 *     refresh during the GET), so the caller must not write; ``"stale"`` when a
 *     refresh landed during the GET; ``"write"`` when the save may proceed.
 */
export type FinishServerCheck = (serverContent: string | null) => "write" | "conflict" | "stale";

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
   * Start a pre-write conflict check, before GETting the file outside the
   * query. Call the returned function with what the GET returned.
   *
   * A refresh that lands during the GET may be newer than its response, and
   * the editor may have changed meanwhile, so the check then reports "stale":
   * the caller should start its save over from the current buffer.
   */
  beginServerCheck: () => FinishServerCheck;
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
  // Counts server content the query delivered, so a pre-write check can tell
  // whether a refresh landed while its GET was in flight.
  const serverVersionRef = useRef(0);

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
    serverVersionRef.current += 1;
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
  const beginServerCheck = useCallback((): FinishServerCheck => {
    const version = serverVersionRef.current;
    return (serverContent) => {
      // A pending conflict, possibly raised by a refresh during the GET, blocks
      // the write until the user picks Keep mine or Load latest.
      if (pendingContentRef.current !== null) return "conflict";
      if (serverVersionRef.current !== version) return "stale";
      // Unknown (failed GET), known (last-seen server state or our own save), or
      // nothing to conflict with.
      if (
        serverContent === null ||
        serverContent === prevContentRef.current ||
        serverContent === lastSavedRef.current ||
        !isDirtyRef.current
      ) {
        return "write";
      }
      // Adopt as the latest known server state — this also lets a later "Keep
      // mine" overwrite through, while a *newer* external edit re-raises.
      prevContentRef.current = serverContent;
      pendingContentRef.current = serverContent;
      setHasExternalUpdate(true);
      return "conflict";
    };
  }, []);

  return {
    editorKey,
    isDirty,
    setDirty,
    hasExternalUpdate,
    discardAndApplyExternal,
    dismissExternalUpdate,
    markSaved,
    beginServerCheck,
  };
}
