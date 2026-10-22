// File drag-and-drop scoped to one element: files dropped anywhere inside it
// attach to the composer, while the shell around it keeps its own behavior.

import { useEffect, useRef, useState } from "react";

/** Files are only readable on drop, so `types` is the only mid-drag signal. */
function carriesFiles(transfer: DataTransfer | null | undefined): boolean {
  if (!transfer) return false;
  return Array.from(transfer.types ?? []).includes("Files");
}

/**
 * Bind file drag-and-drop on ``target`` and report whether such a drag is in
 * flight over it. Non-file drags (text, links) are left to the browser. A null
 * target binds nothing.
 */
export function useFileDropTarget(
  target: HTMLElement | null,
  onFiles: (files: File[]) => void,
): boolean {
  const [isDragActive, setIsDragActive] = useState(false);
  // A rebind between dragenter and drop would lose the depth count.
  const onFilesRef = useRef(onFiles);
  onFilesRef.current = onFiles;

  useEffect(() => {
    if (!target) return;
    // Enter/leave fire in pairs as the pointer crosses child elements.
    let depth = 0;

    const enter = (e: DragEvent): void => {
      if (!carriesFiles(e.dataTransfer)) return;
      depth += 1;
      setIsDragActive(true);
    };

    const leave = (e: DragEvent): void => {
      if (!carriesFiles(e.dataTransfer)) return;
      depth = Math.max(0, depth - 1);
      if (depth === 0) setIsDragActive(false);
    };

    const over = (e: DragEvent): void => {
      if (!carriesFiles(e.dataTransfer)) return;
      // Without this no drop event fires, and the browser opens the file.
      e.preventDefault();
      // A drag entering from another window can arrive without a dragenter.
      setIsDragActive(true);
    };

    const drop = (e: DragEvent): void => {
      if (!carriesFiles(e.dataTransfer)) return;
      e.preventDefault();
      depth = 0;
      setIsDragActive(false);
      const files = Array.from(e.dataTransfer?.files ?? []);
      if (files.length > 0) onFilesRef.current(files);
    };

    // Cancelled drag: Esc, or a drop outside the target.
    const end = (): void => {
      depth = 0;
      setIsDragActive(false);
    };

    target.addEventListener("dragenter", enter);
    target.addEventListener("dragleave", leave);
    target.addEventListener("dragover", over);
    target.addEventListener("drop", drop);
    target.addEventListener("dragend", end);
    return () => {
      target.removeEventListener("dragenter", enter);
      target.removeEventListener("dragleave", leave);
      target.removeEventListener("dragover", over);
      target.removeEventListener("drop", drop);
      target.removeEventListener("dragend", end);
      setIsDragActive(false);
    };
  }, [target]);

  return isDragActive;
}
