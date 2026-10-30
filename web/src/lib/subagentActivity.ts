export interface SubagentActivity {
  phase: "delegated" | "returned";
  sessionId: string;
  title: string;
  status: string | null;
}

/** Read display-only child lifecycle metadata from a persisted resource event. */
export function readSubagentActivity(item: Record<string, unknown>): SubagentActivity | null {
  if (item.type !== "resource_event" || item.resource_type !== "session") return null;
  const phase =
    item.event_type === "session.subagent.delegated"
      ? "delegated"
      : item.event_type === "session.subagent.returned"
        ? "returned"
        : null;
  if (!phase || typeof item.resource_id !== "string" || !item.resource_id) return null;
  const resource = item.resource;
  const title =
    resource && typeof resource === "object" && "title" in resource ? resource.title : null;
  const status =
    resource && typeof resource === "object" && "status" in resource ? resource.status : null;
  return {
    phase,
    sessionId: item.resource_id,
    title: typeof title === "string" && title.trim() ? title : "Sub-agent",
    status: typeof status === "string" ? status : null,
  };
}
