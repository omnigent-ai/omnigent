import type {
  ExtensionContext,
  ExtensionSessionSummary,
} from "@omnigent/extension-sdk";

export function canReadSessions(context: ExtensionContext): boolean {
  return context.capabilities.includes("sessions.listPage");
}

export async function loadSessions(
  context: ExtensionContext,
): Promise<ExtensionSessionSummary[]> {
  if (!canReadSessions(context)) {
    throw new Error("Canvas requires the sessions.read permission");
  }
  return context.sessions.listAll({ pageLimit: 25 });
}
