import type { ViewDefinition, ViewEntity, ViewProperty, ViewRecord } from "@/lib/workViewsApi";

export const ENTITY_NAMES: Record<ViewEntity, string> = {
  tasks: "Tasks",
  projects: "Projects",
  sessions: "Sessions",
};
export const TASK_STATUSES = {
  backlog: "Backlog",
  todo: "Todo",
  in_progress: "In progress",
  review: "In review",
  done: "Done",
  canceled: "Canceled",
};
export const SESSION_STATUSES = {
  needs_input: "Needs input",
  running: "Running",
  waiting: "Waiting",
  idle: "Idle",
  offline: "Offline",
  failed: "Failed",
  archived: "Archived",
};
export const PRIORITIES: Record<number, string> = {
  0: "No priority",
  1: "Urgent",
  2: "High",
  3: "Medium",
  4: "Low",
};
export const PROPERTY_NAMES: Record<ViewProperty, string> = {
  status: "Status",
  project: "Project",
  priority: "Priority",
  created_at: "Created",
  updated_at: "Updated",
};

export function statusLabel(status?: string): string {
  return (
    ({ ...TASK_STATUSES, ...SESSION_STATUSES } as Record<string, string>)[status ?? ""] ??
    status ??
    "No status"
  );
}

export function statusColor(status?: string): string {
  if (status === "done") return "text-green-600 dark:text-green-400";
  if (status === "failed" || status === "needs_input") return "text-red-600 dark:text-red-400";
  if (["in_progress", "running", "waiting"].includes(status ?? ""))
    return "text-amber-600 dark:text-amber-400";
  if (status === "review") return "text-violet-600 dark:text-violet-400";
  return "text-muted-foreground";
}

export interface RecordGroup {
  key: string;
  title: string;
  records: ViewRecord[];
}

export function groupViewRecords(records: ViewRecord[], definition: ViewDefinition): RecordGroup[] {
  const { group_by, show_empty_groups } = definition.display;
  if (group_by === "none")
    return [{ key: "all", title: ENTITY_NAMES[definition.entity_type], records }];
  const groups = new Map<string, RecordGroup>();
  if (show_empty_groups && group_by === "status") {
    for (const [key, title] of Object.entries(
      definition.entity_type === "tasks" ? TASK_STATUSES : SESSION_STATUSES,
    )) {
      groups.set(key, { key, title, records: [] });
    }
  }
  if (show_empty_groups && group_by === "priority") {
    for (const key of [1, 2, 3, 4, 0])
      groups.set(String(key), { key: String(key), title: PRIORITIES[key], records: [] });
  }
  for (const record of records) {
    const key =
      group_by === "project"
        ? (record.project_id ?? "none")
        : group_by === "priority"
          ? String(record.priority ?? 0)
          : (record.status ?? "none");
    const title =
      group_by === "project"
        ? (record.project_name ?? "No project")
        : group_by === "priority"
          ? PRIORITIES[record.priority ?? 0]
          : statusLabel(record.status);
    if (!groups.has(key)) groups.set(key, { key, title, records: [] });
    groups.get(key)!.records.push(record);
  }
  const order =
    group_by === "status"
      ? Object.keys(definition.entity_type === "tasks" ? TASK_STATUSES : SESSION_STATUSES)
      : ["1", "2", "3", "4", "0"];
  return [...groups.values()].sort((left, right) =>
    group_by === "project"
      ? left.title.localeCompare(right.title)
      : order.indexOf(left.key) - order.indexOf(right.key),
  );
}
