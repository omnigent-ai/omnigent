// Right-side push panel that hosts the working-folder file tree.
// Triggered from `SessionRail`'s files card (or the mobile FAB).
//
// Layout contract matches `ExecutionLogsPanel` / `TerminalsPanel`:
//
//   - **Mobile (`< md`)**: fixed full-screen overlay. Slides in from
//     the right via `translate-x`.
//   - **Desktop (`md+`)**: static flex sibling with a resize handle
//     on its left edge. Width is set inline via `useResizablePanel`.
//
// The drawer renders the existing `FilesPanel` in its full-screen
// mode (the `onClose` prop switches it to that layout) so the
// drawer doesn't duplicate the panel's header chrome.

import { useEffect, useRef } from "react";
import {
  ActionScopeProvider,
  HANDLED,
  NOT_HANDLED,
  useActionScopeRegistration,
  useRegisterAction,
} from "@/actions";

import { useResizablePanel } from "@/hooks/useResizablePanel";
import { cn } from "@/lib/utils";
import { FilesPanel } from "./FilesPanel";
import type { ChangedSort } from "./FlatFileList";

interface FilesPanelDrawerProps {
  open: boolean;
  onClose: () => void;
  /**
   * File-select callback. Selecting a file inside the drawer
   * routes to the FileViewer in AppShell; AppShell is responsible
   * for closing this drawer in response.
   */
  onFileSelect: (path: string) => void;
  /**
   * Which scope the drawer renders: false = full folder tree, true =
   * changed-files-only flat list. Fixed by the FAB entry that opened it
   * (Files vs Changes) — the drawer has no in-panel scope switch.
   */
  flatView: boolean;
  /**
   * Lifted hidden-files toggle state. Lifted to AppShell so the
   * eye-icon choice survives inline→drawer transitions.
   */
  showHidden: boolean;
  onShowHiddenChange: (showHidden: boolean) => void;
  /**
   * Lifted changed-files sort order. Lifted to AppShell so the choice
   * stays in sync with the FileViewer's prev/next navigation order.
   */
  sort: ChangedSort;
  onSortChange: (sort: ChangedSort) => void;
}

export function FilesPanelDrawer({
  open,
  onClose,
  onFileSelect,
  flatView,
  showHidden,
  onShowHiddenChange,
  sort,
  onSortChange,
}: FilesPanelDrawerProps) {
  const { panelWidth, handleProps, isDesktop } = useResizablePanel(open);
  const ref = useRef<HTMLElement>(null);
  const actionScope = useActionScopeRegistration({ mode: "filesPanel", active: open });
  useRegisterAction("panel.action.closeFiles", {
    scope: actionScope.id,
    acceptsKeybindings: true,
    run: () => {
      if (!open) return NOT_HANDLED;
      onClose();
      return HANDLED;
    },
  });

  useEffect(() => {
    if (ref.current) {
      if (open) {
        ref.current.removeAttribute("inert");
      } else {
        ref.current.setAttribute("inert", "");
      }
    }
  }, [open]);

  return (
    <aside
      {...actionScope.rootProps}
      ref={ref}
      data-testid="files-panel-drawer"
      data-state={open ? "open" : "closed"}
      style={{ width: panelWidth }}
      className={cn(
        "flex flex-col overflow-hidden bg-card transition-[translate,border-color,border-width] duration-150 ease-out",
        "fixed inset-0 z-50 shadow-lg",
        open ? "translate-x-0" : "translate-x-full",
        "md:relative md:inset-auto md:z-auto md:shadow-none md:translate-x-0 md:shrink-0",
        open ? "md:border-border md:border-l" : "md:w-0 md:border-l-0",
      )}
      aria-hidden={!open}
      data-collapsed={!open || undefined}
    >
      <ActionScopeProvider scope={actionScope}>
        {isDesktop && (
          <div
            {...handleProps}
            className="absolute inset-y-0 left-0 z-10 w-1 cursor-col-resize hover:bg-primary/30 active:bg-primary/50 transition-colors"
          />
        )}
        {/* FilesPanel switches to its full-screen layout when `onClose`
          is set: it owns the header (title + X close button) and
          fills the parent's height. Mount it only while open so the
          folder tree initializes from the latest inline-panel state. */}
        {open && (
          <FilesPanel
            onFileSelect={onFileSelect}
            flatView={flatView}
            showHidden={showHidden}
            onShowHiddenChange={onShowHiddenChange}
            sort={sort}
            onSortChange={onSortChange}
            onClose={onClose}
          />
        )}
      </ActionScopeProvider>
    </aside>
  );
}
