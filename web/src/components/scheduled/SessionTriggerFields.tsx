// The "Session events" trigger: which Omnigent session events start a run, and
// which sessions count. Replaces the schedule fields for such an automation.

import { useId } from "react";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/scheduled/Label";
import {
  SESSION_TRIGGER_EVENTS,
  type SessionTrigger,
  type SessionTriggerEvent,
} from "@/lib/scheduledTasksApi";
import { SESSION_TRIGGER_EVENT_LABELS } from "@/lib/sessionTriggers";

/** A new session trigger's starting point: a run when a turn finishes. */
export const DEFAULT_SESSION_TRIGGER: SessionTrigger = {
  events: ["turn.completed"],
  titleContains: null,
  includeOwnSessions: false,
};

export function SessionTriggerFields({
  value,
  onChange,
  disabled = false,
}: {
  value: SessionTrigger;
  onChange: (next: SessionTrigger) => void;
  disabled?: boolean;
}) {
  const id = useId();
  const selected = new Set(value.events);

  function toggle(event: SessionTriggerEvent, checked: boolean) {
    const next = new Set(selected);
    if (checked) next.add(event);
    else next.delete(event);
    // Keep the form's order, so a saved trigger reads the same way back.
    onChange({ ...value, events: SESSION_TRIGGER_EVENTS.filter((e) => next.has(e)) });
  }

  return (
    <div className="flex flex-col gap-3" data-testid="task-session-trigger-fields">
      <fieldset className="flex flex-col gap-1.5" disabled={disabled}>
        <legend className="mb-1.5 text-sm font-medium">Run when</legend>
        {SESSION_TRIGGER_EVENTS.map((event) => (
          <div key={event} className="flex items-center gap-2">
            <Checkbox
              id={`${id}-${event}`}
              checked={selected.has(event)}
              onCheckedChange={(checked) => toggle(event, checked === true)}
              data-testid={`task-session-event-${event}`}
              componentId={`tasks.scheduled.session_event.${event}`}
            />
            <label htmlFor={`${id}-${event}`} className="cursor-pointer text-sm">
              {SESSION_TRIGGER_EVENT_LABELS[event]}
            </label>
          </div>
        ))}
        {value.events.length === 0 && (
          <p role="status" className="text-sm text-warning">
            Choose at least one event.
          </p>
        )}
      </fieldset>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${id}-title`}>Only sessions whose title contains</Label>
        <Input
          id={`${id}-title`}
          value={value.titleContains ?? ""}
          placeholder="Any session"
          maxLength={256}
          disabled={disabled}
          data-testid="task-session-title-filter"
          className="text-ui focus-visible:ring-0"
          onChange={(e) => onChange({ ...value, titleContains: e.target.value || null })}
        />
      </div>
      <div className="flex items-center justify-between gap-4">
        <Label htmlFor={`${id}-own`}>Include sessions this automation started</Label>
        <Switch
          id={`${id}-own`}
          checked={value.includeOwnSessions === true}
          disabled={disabled}
          onCheckedChange={(checked) => onChange({ ...value, includeOwnSessions: checked })}
          data-testid="task-session-include-own"
          componentId="tasks.scheduled.session_include_own"
        />
      </div>
      <p className="text-sm text-muted-foreground">
        Runs only for sessions you own or that are shared with you, at most 10 times a minute. Each
        run gets the session IDs, not their content; the agent reads them with its session tools.
      </p>
    </div>
  );
}
