import { useCallback, useEffect, useMemo, useSyncExternalStore } from "react";

import type { ProjectSummary } from "@/hooks/useConversations";
import {
  NEW_SESSION_TARGET_CHANGED_EVENT,
  NEW_SESSION_TARGET_STORAGE_KEY,
  type NewSessionTarget,
  newSessionRoute,
  parseNewSessionTarget,
  resolveNewSessionTarget,
  writeNewSessionTarget,
} from "@/lib/newSessionTarget";

function subscribe(onStoreChange: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  window.addEventListener(NEW_SESSION_TARGET_CHANGED_EVENT, onStoreChange);
  window.addEventListener("storage", onStoreChange);
  return () => {
    window.removeEventListener(NEW_SESSION_TARGET_CHANGED_EVENT, onStoreChange);
    window.removeEventListener("storage", onStoreChange);
  };
}

function getSnapshot(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(NEW_SESSION_TARGET_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export interface UseNewSessionTargetResult {
  target: NewSessionTarget;
  route: string;
  selectNoProject: () => void;
  selectProject: (project: ProjectSummary) => void;
}

/** Reactive selected destination shared by every global new-session entry. */
export function useNewSessionTarget(
  projects?: readonly ProjectSummary[],
): UseNewSessionTargetResult {
  const raw = useSyncExternalStore(subscribe, getSnapshot, () => "");
  const stored = useMemo(() => parseNewSessionTarget(raw), [raw]);
  const target = useMemo(() => resolveNewSessionTarget(stored, projects), [stored, projects]);

  useEffect(() => {
    if (projects === undefined || stored.kind === "none") return;
    if (target.kind === "none") {
      writeNewSessionTarget(target);
      return;
    }
    if (target.projectId !== stored.projectId || target.projectName !== stored.projectName) {
      writeNewSessionTarget(target);
    }
  }, [projects, stored, target]);

  const selectNoProject = useCallback(() => writeNewSessionTarget({ kind: "none" }), []);
  const selectProject = useCallback((project: ProjectSummary) => {
    writeNewSessionTarget({
      kind: "project",
      projectId: project.id,
      projectName: project.name,
    });
  }, []);

  return { target, route: newSessionRoute(target), selectNoProject, selectProject };
}
