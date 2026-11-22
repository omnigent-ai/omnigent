// A Files row's right-click menu and ⌘K target: the shared file actions for the
// row's path. Spread `rowProps` on the row and render `menu` inside it.

import { useRef, useState, type MouseEvent } from "react";
import { createPortal } from "react-dom";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { FileCommandMenuItem } from "./FileCommandMenuItem";
import { useFocusedFileSource } from "./focusedFile";
import { type FileCommandTarget, useFileCommandTarget } from "./target";
import { type FileCommandOptions, useFileCommandsForTarget } from "./useFileCommands";

/** A tree's ways to open a row besides selecting it, offered as the shared file actions. */
export type FileRowOpens = Pick<FileCommandOptions, "onOpenInNewTab" | "onOpenAsRoot">;

interface FileRowOptions {
  directory?: boolean;
  deleted?: boolean;
  /** The surface's own ways to open the row, such as rooting the tree at a folder. */
  opens?: FileRowOpens;
}

export function useFileCommandMenu(
  path: string,
  { directory = false, deleted = false, opens }: FileRowOptions = {},
) {
  const [point, setPoint] = useState<{ x: number; y: number } | null>(null);
  const row = useRef<HTMLElement | null>(null);
  const target = useFileCommandTarget(path, { directory, deleted });
  const onFocus = useFocusedFileSource(target);
  const rowProps = {
    onContextMenu: (event: MouseEvent<HTMLElement>) => {
      event.preventDefault();
      row.current = event.currentTarget;
      // A keyboard-opened menu has no pointer position; anchor it to the row.
      const box = event.currentTarget.getBoundingClientRect();
      setPoint({ x: event.clientX || box.left, y: event.clientY || box.bottom });
    },
    // The row's path becomes ⌘K's file while keyboard focus is in it.
    onFocus,
  };
  const menu = point && target && (
    <FileCommandMenuAt
      target={target}
      point={point}
      onClose={() => setPoint(null)}
      onRestoreFocus={() => row.current?.focus({ preventScroll: true })}
      opens={opens}
    />
  );
  // `menuOpen` lets the row hide its hover tooltip, which would cover the menu.
  return { rowProps, menu, menuOpen: point !== null };
}

/**
 * Where focus goes when a row's menu closes. Escape, or a command that leaves focus nowhere,
 * returns it to the row through `restoreFocus`; a click elsewhere, or a command that moved
 * focus on purpose, such as into a dialog, leaves it where it went.
 */
export function useMenuFocusReturn(restoreFocus: () => void) {
  const interactedOutside = useRef(false);
  return {
    onInteractOutside: () => {
      interactedOutside.current = true;
    },
    onCloseAutoFocus: (event: Event) => {
      event.preventDefault();
      if (interactedOutside.current) return;
      requestAnimationFrame(() => {
        const active = document.activeElement;
        if (!active || active === document.body) restoreFocus();
      });
    },
  };
}

/** A Files row's menu for `target`, open at `point` until it closes. */
export function FileCommandMenuAt({
  target,
  point,
  onClose,
  onRestoreFocus,
  opens,
}: {
  target: FileCommandTarget;
  point: { x: number; y: number };
  onClose: () => void;
  /** Returns focus to the row the menu opened from. */
  onRestoreFocus: () => void;
  /** The tree's own ways to open the row, such as in a new tab. */
  opens?: FileRowOpens;
}) {
  const focusReturn = useMenuFocusReturn(onRestoreFocus);
  return createPortal(
    <DropdownMenu open modal={false} onOpenChange={(open) => !open && onClose()}>
      <DropdownMenuTrigger asChild>
        <span className="fixed" style={{ left: point.x, top: point.y }} />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" {...focusReturn}>
        <FileRowMenuItems target={target} opens={opens} />
      </DropdownMenuContent>
    </DropdownMenu>,
    document.body,
  );
}

/** Mounted only while the menu is open, so closed rows don't derive their actions. */
function FileRowMenuItems({ target, opens }: { target: FileCommandTarget; opens?: FileRowOpens }) {
  // The row opens its file itself, so its menu doesn't repeat that.
  const { commands, run } = useFileCommandsForTarget(target, opens);
  return commands.map((command) => (
    <FileCommandMenuItem key={command.id} command={command} onRun={run} Item={DropdownMenuItem} />
  ));
}
