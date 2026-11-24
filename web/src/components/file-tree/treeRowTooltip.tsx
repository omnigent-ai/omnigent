// A file tree row's full path in a tooltip that follows the pointer, so a name the
// tree truncated in the middle can still be read in full.

import { type ReactNode, type RefObject, useEffect, useRef, useState } from "react";
import { FILE_TREE_ROOT_SELECTOR } from "@/components/file-tree/vendored";
import { rowPathOf } from "@/shell/fileTree/treeRowPath";
import { useCursorTooltip } from "@/shell/useCursorTooltip";

/** A row's tooltip text: its path, without a folder's trailing slash. */
export function treeRowTooltipText(treePath: string): string {
  return treePath.replace(/\/+$/, "");
}

/**
 * The tooltip for the row under the pointer in the tree inside `container`, or nothing off
 * the rows and while `hidden`. `onHover` hears each row the pointer moves onto, or null.
 * It listens on the tree drawn for `model`, as the tree's other host listeners do.
 */
export function useTreeRowTooltip(
  container: RefObject<HTMLElement | null>,
  model: object,
  {
    hidden = false,
    onHover,
  }: { hidden?: boolean; onHover?: (treePath: string | null) => void } = {},
): ReactNode {
  const [hovered, setHovered] = useState<string | null>(null);
  const { handlers, tooltip } = useCursorTooltip(hovered ? treeRowTooltipText(hovered) : "");
  const latest = useRef({ handlers, onHover });
  latest.current = { handlers, onHover };

  useEffect(() => {
    const host = container.current?.querySelector<HTMLElement>(FILE_TREE_ROOT_SELECTOR);
    if (!host) return;
    // The hovered row follows the pointer's moves: entering a row isn't always reported.
    let current: string | null = null;
    const onPointerMove = (event: PointerEvent) => {
      latest.current.handlers.onMouseMove(event as unknown as React.MouseEvent);
      const path = rowPathOf(event);
      if (path === current) return;
      current = path;
      setHovered(path);
      latest.current.onHover?.(path);
    };
    const onPointerLeave = () => {
      current = null;
      setHovered(null);
      latest.current.onHover?.(null);
      latest.current.handlers.onMouseLeave();
    };
    host.addEventListener("pointermove", onPointerMove);
    host.addEventListener("pointerleave", onPointerLeave);
    return () => {
      host.removeEventListener("pointermove", onPointerMove);
      host.removeEventListener("pointerleave", onPointerLeave);
    };
  }, [container, model]);

  return hovered && !hidden ? tooltip : null;
}
