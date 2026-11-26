// What a viewer sees on an automation someone else shared with them: whose it
// is, and why each action that changes it is disabled.

import type { ScheduledTask } from "@/lib/scheduledTasksApi";

/** Tooltips for the actions only an automation's owner may take. */
export const OWNER_ONLY_REASONS = {
  runNow: "Only the automation's owner can run it.",
  edit: "Only the automation's owner can edit it.",
  share: "Only the automation's owner can share it.",
  clone: "Only the automation's owner can clone it.",
  pause: "Only the automation's owner can pause it.",
  resume: "Only the automation's owner can resume it.",
  delete: "Only the automation's owner can delete it.",
} as const;

/** Whether the viewer only reads this automation: it is shared with them. */
export function isSharedWithViewer(task: Pick<ScheduledTask, "viewerRole">): boolean {
  return task.viewerRole === "viewer";
}

/** The owner's name as the server gives it to a viewer, else their ID. */
export function automationOwnerName(
  task: Pick<ScheduledTask, "ownerDisplayName" | "ownerUserId">,
): string {
  return task.ownerDisplayName || task.ownerUserId || "another user";
}
