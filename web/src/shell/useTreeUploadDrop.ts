import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { useFileDropTarget } from "@/hooks/useFileDropTarget";
import { resolveUploadTarget } from "./WorkspaceUploads";

export interface TreeDropHover {
  directory: string;
  row: HTMLElement | null;
  folderRow: HTMLElement | null;
}

const SPRING_LOAD_DELAY_MS = 1000;

function folderRowFor(target: HTMLElement | null, row: HTMLElement | null): HTMLElement | null {
  if (!row) return null;
  if (row.hasAttribute("data-drop-folder")) return row;
  return (
    Array.from(target?.querySelectorAll<HTMLElement>("[data-drop-folder]") ?? []).find(
      (folder) => folder.dataset.dropDir === row.dataset.dropDir,
    ) ?? null
  );
}

export function useTreeUploadDrop(
  scrollRef: RefObject<HTMLElement | null>,
  options: {
    enabled: boolean;
    directory: string;
    onFiles: (files: File[], event: DragEvent) => void;
    onExpand: (path: string) => void;
  },
) {
  const [target, setTarget] = useState<HTMLElement | null>(null);
  const [hover, setHover] = useState<TreeDropHover | null>(null);
  const currentHover = useRef<TreeDropHover | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const latest = useRef(options);
  latest.current = options;
  useLayoutEffect(() => setTarget(scrollRef.current), [scrollRef, options.enabled]);
  const clear = useCallback(() => {
    clearTimeout(timer.current);
    timer.current = undefined;
    currentHover.current = null;
    setHover(null);
  }, []);
  const active = useFileDropTarget(options.enabled ? target : null, options.onFiles, {
    clearOnScroll: true,
    onClear: clear,
    onOver: (event) => {
      const row =
        event.target instanceof Element
          ? event.target.closest<HTMLElement>("[data-drop-dir]")
          : null;
      const directory = resolveUploadTarget(event, options.directory);
      if (currentHover.current?.row === row && currentHover.current.directory === directory) return;
      const folderRow = folderRowFor(target, row);
      const rowChanged = row !== currentHover.current?.row;
      currentHover.current = { directory, row, folderRow };
      setHover(currentHover.current);
      if (!rowChanged) return;
      clearTimeout(timer.current);
      if (row?.hasAttribute("data-drop-folder") && row.querySelector('[aria-expanded="false"]')) {
        timer.current = setTimeout(() => {
          if (!row.isConnected || !target?.contains(row)) {
            clear();
            return;
          }
          latest.current.onExpand(row.dataset.dropDir ?? "");
        }, SPRING_LOAD_DELAY_MS);
      }
    },
  });
  useEffect(() => {
    if (!target || !options.enabled) {
      clear();
      return;
    }
    target.dataset.uploadTree = "";
    const observer = new MutationObserver(() => {
      const hovered = currentHover.current;
      if (!hovered?.row) return;
      if (!target.contains(hovered.row)) {
        clear();
        return;
      }
      const folderRow = folderRowFor(target, hovered.row);
      if (folderRow !== hovered.folderRow) {
        currentHover.current = { ...hovered, folderRow };
        setHover(currentHover.current);
      }
    });
    observer.observe(target, { childList: true, subtree: true });
    return () => {
      delete target.dataset.uploadTree;
      observer.disconnect();
      clear();
    };
  }, [target, options.enabled, options.directory, clear]);
  return { active, hover: active ? hover : null };
}
