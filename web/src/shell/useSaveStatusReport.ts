// Reports a code editor's auto-save lifecycle to the FileViewer toolbar chip,
// shared by every editor engine so the chip reads the same everywhere.

import { useEffect, useRef } from "react";
import type { SaveStatus } from "./codeViewerHelpers";

// How long the transient "Saved" badge stays up before the chip clears itself.
export const SAVED_BADGE_MS = 2000;

interface SaveStatusInputs {
  writePending: boolean;
  writeError: boolean;
  writeSuccess: boolean;
  saveDisabled: boolean;
  isDirty: boolean;
}

/**
 * The chip's status. A failed or in-flight write outranks the dirty flag, and
 * edits outrank a stale "saved"; a failure clears once the buffer is clean.
 */
export function saveStatusOf({
  writePending,
  writeError,
  writeSuccess,
  saveDisabled,
  isDirty,
}: SaveStatusInputs): SaveStatus {
  if (writeError && isDirty) return "error";
  if (writePending) return "saving";
  if (saveDisabled && isDirty) return "offline";
  if (isDirty) return "unsaved";
  if (writeSuccess) return "saved";
  return "idle";
}

export function useSaveStatusReport(
  inputs: SaveStatusInputs,
  onSaveStatusChange: ((status: SaveStatus) => void) | undefined,
): void {
  // Ref'd so a fresh callback identity from the parent doesn't re-run the effect.
  const onChangeRef = useRef(onSaveStatusChange);
  onChangeRef.current = onSaveStatusChange;
  const status = saveStatusOf(inputs);
  useEffect(() => {
    onChangeRef.current?.(status);
    if (status !== "saved") return undefined;
    const timeout = window.setTimeout(() => onChangeRef.current?.("idle"), SAVED_BADGE_MS);
    return () => window.clearTimeout(timeout);
    // Re-run on every input change so a repeat save shows "Saved" again.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    inputs.writePending,
    inputs.writeError,
    inputs.writeSuccess,
    inputs.saveDisabled,
    inputs.isDirty,
  ]);
  // A stale "Saving…" or "Saved" must not outlive the editor.
  useEffect(() => () => onChangeRef.current?.("idle"), []);
}
