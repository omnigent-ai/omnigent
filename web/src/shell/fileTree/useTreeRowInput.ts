// A file tree's row input, caught on its root before the tree sees it. A file opens once
// per click, Enter or Space on it; ⌘-click (Ctrl-click off macOS) or a middle click opens
// it in a new tab; range and toggle selection keep to one row, so no selection opens a file.

import { type RefObject, useEffect, useRef } from "react";
import { FILE_TREE_ROOT_SELECTOR } from "@/components/file-tree/vendored";
import { rowPathOf } from "./treeRowPath";
import { type SingleSelection, opensRow, rowClickAction, rowKeyAction } from "./treeSelection";

/** The rendered tree inside `container`, if it has rendered. */
export const treeRoot = (container: ParentNode | null | undefined): HTMLElement | null =>
  container?.querySelector<HTMLElement>(FILE_TREE_ROOT_SELECTOR) ?? null;

/**
 * Opens the rows a person activates in the tree rendered inside `containerRef`.
 *
 * :param selection: Keeps the tree to one selected row.
 * :param activate: Opens a row, in a new tab when `newTab` is set; folders reach it too.
 */
export function useTreeRowInput(
  containerRef: RefObject<HTMLElement | null>,
  selection: SingleSelection,
  activate: (path: string, newTab: boolean) => void,
): void {
  const activateRef = useRef(activate);
  activateRef.current = activate;
  useEffect(() => {
    const host = treeRoot(containerRef.current);
    if (!host) return;
    // Rows are native buttons, so a key that opens one holds back the button's own click.
    const onKeyDown = (event: KeyboardEvent) => {
      const path = rowPathOf(event);
      if (path === null) return;
      if (selection.apply(rowKeyAction(event)).consumed) {
        event.preventDefault();
        event.stopPropagation();
        return;
      }
      if (!opensRow(event) || path.endsWith("/")) return;
      event.preventDefault();
      selection.selectOnly(path);
      activateRef.current(path, false);
    };
    const onClick = (event: MouseEvent) => {
      const path = rowPathOf(event);
      const result = selection.apply(rowClickAction(event, path));
      if (!result.consumed) {
        if (path) activateRef.current(path, false);
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      if (result.activate) activateRef.current(result.activate, result.newTab);
    };
    // A middle click opens a file in its own tab, as in a browser.
    const onAuxClick = (event: MouseEvent) => {
      const path = rowPathOf(event);
      if (event.button !== 1 || !path || path.endsWith("/")) return;
      event.preventDefault();
      activateRef.current(path, true);
    };
    host.addEventListener("keydown", onKeyDown, true);
    host.addEventListener("click", onClick, true);
    host.addEventListener("auxclick", onAuxClick);
    return () => {
      host.removeEventListener("keydown", onKeyDown, true);
      host.removeEventListener("click", onClick, true);
      host.removeEventListener("auxclick", onAuxClick);
    };
  }, [containerRef, selection]);
}
