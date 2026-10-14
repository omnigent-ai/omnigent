import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { useFileDropTarget } from "@/hooks/useFileDropTarget";
import { resolveUploadTarget } from "./WorkspaceUploads";

export interface TreeDropHover {
  directory: string;
  row: HTMLElement | null;
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
      const rowChanged = row !== currentHover.current?.row;
      currentHover.current = { directory, row };
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
        }, 600);
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
      if (currentHover.current?.row && !target.contains(currentHover.current.row)) clear();
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
