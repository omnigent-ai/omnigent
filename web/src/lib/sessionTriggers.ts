// Labels and summaries for automations that run on Omnigent session events
// (`trigger_config` provider `omnigent`) instead of an RRULE schedule.

import { describeSchedule, EVENT_TRIGGER_SUMMARY } from "./scheduleText";
import type { ScheduledTask, SessionTrigger, SessionTriggerEvent } from "./scheduledTasksApi";

/** Picker labels for each event, in the order the form lists them. */
export const SESSION_TRIGGER_EVENT_LABELS: Record<SessionTriggerEvent, string> = {
  "turn.completed": "A turn finishes",
  "turn.failed": "A turn fails",
  "input.requested": "The agent asks for approval or input",
  "comment.created": "Someone comments",
  "session.created": "A session is created",
  "session.renamed": "A session is renamed",
  "session.archived": "A session is archived",
  "session.deleted": "A session is deleted",
};

// Short forms for one-line summaries, e.g. "When a turn finishes or fails".
const SUMMARY_PHRASES: Record<SessionTriggerEvent, string> = {
  "turn.completed": "a turn finishes",
  "turn.failed": "a turn fails",
  "input.requested": "an agent asks for input",
  "comment.created": "someone comments",
  "session.created": "a session is created",
  "session.renamed": "a session is renamed",
  "session.archived": "a session is archived",
  "session.deleted": "a session is deleted",
};

/** One-line summary of a session trigger, e.g. "When a turn finishes or a turn fails". */
export function describeSessionTrigger(trigger: SessionTrigger): string {
  const phrases = trigger.events.map((event) => SUMMARY_PHRASES[event] ?? event);
  if (phrases.length === 0) return "On session events";
  const joined =
    phrases.length === 1
      ? phrases[0]
      : `${phrases.slice(0, -1).join(", ")} or ${phrases[phrases.length - 1]}`;
  const title = trigger.titleContains ? ` in sessions titled "${trigger.titleContains}"` : "";
  return `When ${joined}${title}`;
}

/** What starts an automation's runs: its schedule, or the session events it watches. */
export function describeTaskTrigger(task: Pick<ScheduledTask, "rrule" | "sessionTrigger">): string {
  if (task.sessionTrigger) return describeSessionTrigger(task.sessionTrigger);
  return task.rrule !== null ? describeSchedule(task.rrule) : EVENT_TRIGGER_SUMMARY;
}
