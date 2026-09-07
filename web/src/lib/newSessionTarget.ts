import type { ProjectSummary } from "@/hooks/useConversations";

export const NEW_SESSION_TARGET_STORAGE_KEY = "omnigent:new-session-target";
export const NEW_SESSION_TARGET_CHANGED_EVENT = "omnigent:new-session-target-changed";

export type NewSessionTarget =
  { kind: "none" } | { kind: "project"; projectId: string | null; projectName: string };

export const NO_PROJECT_NEW_SESSION_TARGET: NewSessionTarget = { kind: "none" };

function normalizeProjectTarget(value: unknown): NewSessionTarget | null {
  if (!value || typeof value !== "object") return null;
  const candidate = value as Partial<Extract<NewSessionTarget, { kind: "project" }>>;
  if (candidate.kind !== "project") return null;
  const projectName = typeof candidate.projectName === "string" ? candidate.projectName.trim() : "";
  if (!projectName) return null;
  const projectId =
    candidate.projectId === null || typeof candidate.projectId === "string"
      ? candidate.projectId
      : null;
  return { kind: "project", projectId, projectName };
}

export function parseNewSessionTarget(raw: string | null): NewSessionTarget {
  if (!raw) return NO_PROJECT_NEW_SESSION_TARGET;
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (parsed && typeof parsed === "object" && (parsed as { kind?: unknown }).kind === "none") {
      return NO_PROJECT_NEW_SESSION_TARGET;
    }
    return normalizeProjectTarget(parsed) ?? NO_PROJECT_NEW_SESSION_TARGET;
  } catch {
    return NO_PROJECT_NEW_SESSION_TARGET;
  }
}

export function readNewSessionTarget(): NewSessionTarget {
  if (typeof window === "undefined") return NO_PROJECT_NEW_SESSION_TARGET;
  try {
    return parseNewSessionTarget(window.localStorage.getItem(NEW_SESSION_TARGET_STORAGE_KEY));
  } catch {
    return NO_PROJECT_NEW_SESSION_TARGET;
  }
}

export function writeNewSessionTarget(target: NewSessionTarget): void {
  if (typeof window === "undefined") return;
  const normalized =
    target.kind === "project" ? normalizeProjectTarget(target) : NO_PROJECT_NEW_SESSION_TARGET;
  try {
    if (!normalized || normalized.kind === "none") {
      window.localStorage.removeItem(NEW_SESSION_TARGET_STORAGE_KEY);
    } else {
      window.localStorage.setItem(NEW_SESSION_TARGET_STORAGE_KEY, JSON.stringify(normalized));
    }
  } catch {
    // Storage denial must not block session creation.
  }
  window.dispatchEvent(new Event(NEW_SESSION_TARGET_CHANGED_EVENT));
}

/** Resolve the persisted identity against the current project list. */
export function resolveNewSessionTarget(
  target: NewSessionTarget,
  projects?: readonly ProjectSummary[],
): NewSessionTarget {
  if (target.kind === "none" || projects === undefined) return target;
  const match =
    target.projectId !== null
      ? projects.find((project) => project.id === target.projectId)
      : projects.find((project) => project.name === target.projectName);
  return match
    ? { kind: "project", projectId: match.id, projectName: match.name }
    : NO_PROJECT_NEW_SESSION_TARGET;
}

export function newSessionRoute(target: NewSessionTarget): string {
  return target.kind === "project" ? `/?project=${encodeURIComponent(target.projectName)}` : "/";
}

export function newSessionTargetLabel(target: NewSessionTarget): string {
  return target.kind === "project" ? target.projectName : "No Project";
}
