// Open state, width and maximize for a shared Workspace panel beside a set of
// sessions (Grid cells, Tile view tiles). Open and width persist per `workspaceKey`.

import { useCallback, useEffect, useRef, useState } from "react";
import { readDefaultWorkspacePanelOpen } from "@/lib/workspacePanelPreferences";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import type { SharedWorkspacePanelProps } from "@/shell/SessionWorkspacePanel";

export function useResizableWorkspacePanel(workspaceKey: string, resizeLabel: string) {
  const [workspace, setWorkspace] = useState(() => {
    const saved = readSessionWorkspaceState(workspaceKey);
    return { open: saved.open ?? readDefaultWorkspacePanelOpen(), widthPx: saved.widthPx };
  });
  useEffect(() => writeSessionWorkspaceState(workspaceKey, workspace), [workspaceKey, workspace]);
  const [maximized, setMaximized] = useState(false);
  const openWorkspace = useCallback(
    () => setWorkspace((previous) => ({ ...previous, open: true })),
    [],
  );
  const toggleWorkspace = () => {
    setMaximized(false);
    setWorkspace((previous) => ({ ...previous, open: !previous.open }));
  };
  const container = useRef<HTMLDivElement>(null);
  const [containerWidth, setContainerWidth] = useState(0);
  useEffect(() => {
    const node = container.current;
    if (!node) return;
    const observer = new ResizeObserver(() => setContainerWidth(node.clientWidth));
    observer.observe(node);
    setContainerWidth(node.clientWidth);
    return () => observer.disconnect();
  }, []);
  const maximumWidth = Math.max(0, containerWidth - 280);
  const width = Math.min(workspace.widthPx ?? Math.min(600, containerWidth * 0.4), maximumWidth);
  const resize = (next: number) =>
    setWorkspace((previous) => ({
      ...previous,
      widthPx: Math.max(Math.min(240, maximumWidth), Math.min(maximumWidth, next)),
    }));
  const workspacePanel: SharedWorkspacePanelProps = {
    open: workspace.open,
    width,
    maximized,
    onToggleMaximized: () => setMaximized((previous) => !previous),
    handleProps: {
      tabIndex: 0,
      role: "separator",
      "aria-label": resizeLabel,
      "aria-orientation": "vertical",
      "aria-valuenow": Math.round(width),
      "aria-valuemin": 0,
      "aria-valuemax": maximumWidth,
      onPointerDown: (event) => {
        event.preventDefault();
        event.currentTarget.setPointerCapture(event.pointerId);
      },
      onPointerMove: (event) => {
        if (event.currentTarget.hasPointerCapture(event.pointerId) && container.current)
          resize(container.current.getBoundingClientRect().right - event.clientX);
      },
      onPointerUp: (event) => event.currentTarget.releasePointerCapture(event.pointerId),
      onKeyDown: (event) => {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        event.preventDefault();
        resize(width + (event.key === "ArrowLeft" ? 24 : -24));
      },
    },
  };
  return {
    container,
    open: workspace.open,
    maximized,
    setMaximized,
    openWorkspace,
    toggleWorkspace,
    workspacePanel,
  };
}
