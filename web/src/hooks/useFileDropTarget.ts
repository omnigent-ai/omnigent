// File drops belong to the surface under the pointer; the shell keeps its own behavior.

import { useEffect, useRef, useSyncExternalStore } from "react";

let activeTarget: HTMLElement | null = null;
const listeners = new Set<() => void>();
function setActiveTarget(target: HTMLElement | null) {
  activeTarget = target;
  for (const listener of listeners) listener();
}
function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export const ignoreWorkspaceUploadDrag = (event: DragEvent): boolean =>
  event.target instanceof Element && event.target.closest("[data-upload-drop-root]") !== null;

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
  onFiles: (files: File[], event: DragEvent) => void,
  options: {
    ignore?: (event: DragEvent) => boolean;
    onOver?: (event: DragEvent) => void;
    onClear?: () => void;
    clearOnScroll?: boolean;
  } = {},
): boolean {
  const owner = useSyncExternalStore(
    subscribe,
    () => activeTarget,
    () => null,
  );
  // A rebind between dragenter and drop would lose the depth count.
  const onFilesRef = useRef(onFiles);
  onFilesRef.current = onFiles;
  const optionsRef = useRef(options);
  optionsRef.current = options;

  useEffect(() => {
    if (!target) return;
    // Enter/leave fire in pairs as the pointer crosses child elements.
    let depth = 0;
    const setIsDragActive = (active: boolean) => {
      if (active) setActiveTarget(target);
      else {
        if (activeTarget === target) setActiveTarget(null);
        optionsRef.current.onClear?.();
      }
    };
    const ignored = (event: DragEvent) => {
      if (!optionsRef.current.ignore?.(event)) return false;
      setIsDragActive(false);
      return true;
    };

    const enter = (e: DragEvent): void => {
      if (!carriesFiles(e.dataTransfer)) return;
      if (ignored(e)) return;
      depth += 1;
      setIsDragActive(true);
      optionsRef.current.onOver?.(e);
    };

    const leave = (e: DragEvent): void => {
      if (!carriesFiles(e.dataTransfer) || optionsRef.current.ignore?.(e)) return;
      depth = Math.max(0, depth - 1);
      if (depth === 0) setIsDragActive(false);
    };

    const over = (e: DragEvent): void => {
      if (!carriesFiles(e.dataTransfer)) return;
      if (ignored(e)) return;
      // Without this no drop event fires, and the browser opens the file.
      e.preventDefault();
      // A drag entering from another window can arrive without a dragenter.
      setIsDragActive(true);
      optionsRef.current.onOver?.(e);
    };

    const drop = (e: DragEvent): void => {
      if (!carriesFiles(e.dataTransfer)) return;
      if (ignored(e)) return;
      e.preventDefault();
      depth = 0;
      setIsDragActive(false);
      const files = Array.from(e.dataTransfer?.files ?? []);
      if (files.length > 0) onFilesRef.current(files, e);
    };

    // Cancelled drag: Esc, or a drop outside the target.
    const end = (): void => {
      depth = 0;
      setIsDragActive(false);
    };

    const scroll = () => {
      if (optionsRef.current.clearOnScroll) setIsDragActive(false);
    };
    target.addEventListener("scroll", scroll);
    target.addEventListener("dragenter", enter);
    target.addEventListener("dragleave", leave);
    target.addEventListener("dragover", over);
    target.addEventListener("drop", drop);
    target.addEventListener("dragend", end);
    const outside = (event: DragEvent) => {
      if (
        carriesFiles(event.dataTransfer) &&
        event.target instanceof Node &&
        !target.contains(event.target)
      )
        end();
    };
    document.addEventListener("dragover", outside);
    document.addEventListener("drop", end);
    document.addEventListener("dragend", end);
    return () => {
      target.removeEventListener("scroll", scroll);
      target.removeEventListener("dragenter", enter);
      target.removeEventListener("dragleave", leave);
      target.removeEventListener("dragover", over);
      target.removeEventListener("drop", drop);
      target.removeEventListener("dragend", end);
      document.removeEventListener("dragover", outside);
      document.removeEventListener("drop", end);
      document.removeEventListener("dragend", end);
      setIsDragActive(false);
    };
  }, [target]);

  return target !== null && owner === target;
}
