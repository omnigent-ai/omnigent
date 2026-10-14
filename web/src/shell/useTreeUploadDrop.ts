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
  const hoveredRow = useRef<HTMLElement | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const latest = useRef(options);
  latest.current = options;
  useLayoutEffect(() => setTarget(scrollRef.current), [scrollRef, options.enabled]);
  const clear = useCallback(() => {
    clearTimeout(timer.current);
    timer.current = undefined;
    hoveredRow.current = null;
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
      setHover({ directory: resolveUploadTarget(event, options.directory), row });
      if (row === hoveredRow.current) return;
      clearTimeout(timer.current);
      hoveredRow.current = row;
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
      if (hoveredRow.current && !target.contains(hoveredRow.current)) clear();
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
