import type {
  ExtensionContext,
  ExtensionProjectSummary,
  ExtensionSessionSummary,
} from "@omnigent/extension-sdk";

export function canReadSessions(context: ExtensionContext): boolean {
  return context.capabilities.includes("sessions.listPage");
}

export function canReadProjects(context: ExtensionContext): boolean {
  return context.capabilities.includes("projects.list");
}

export function canCreateProjects(context: ExtensionContext): boolean {
  return context.capabilities.includes("projects.create");
}

export async function loadSessions(
  context: ExtensionContext,
): Promise<ExtensionSessionSummary[]> {
  if (!canReadSessions(context)) {
    throw new Error("Canvas requires the sessions.read permission");
  }
  return context.sessions.listAll({ pageLimit: 25 });
}

// Without the projects capability every session lands on the Main canvas.
export async function loadProjects(
  context: ExtensionContext,
): Promise<ExtensionProjectSummary[]> {
  if (!canReadProjects(context)) return [];
  return context.projects.list();
}
