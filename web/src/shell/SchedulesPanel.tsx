/**
 * Right-rail Schedules panel — the conversation's loops & monitors (#6).
 *
 * Lists schedules from /v1/schedules, with an enable/disable switch and a
 * delete button per row. Agents create them via the create_loop /
 * create_monitor tools; this is the human management surface.
 */

import { RepeatIcon, TerminalIcon, TrashIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import type { Schedule } from "@/lib/schedulesApi";
import { useDeleteSchedule, useSchedules, useUpdateSchedule } from "@/hooks/useSchedules";

function ScheduleRow({ schedule }: { schedule: Schedule }) {
  const update = useUpdateSchedule();
  const remove = useDeleteSchedule();
  const isLoop = schedule.kind === "loop";

  return (
    <li className="flex items-center gap-3 rounded-lg border px-3 py-2" data-testid="schedule-row">
      {isLoop ? (
        <RepeatIcon className="size-4 shrink-0 text-muted-foreground" />
      ) : (
        <TerminalIcon className="size-4 shrink-0 text-muted-foreground" />
      )}
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium">{schedule.name}</div>
        <div className="truncate font-mono text-xs text-muted-foreground">
          {isLoop ? schedule.cron : schedule.command}
          {schedule.status && schedule.status !== "idle" ? ` · ${schedule.status}` : ""}
        </div>
      </div>
      <Switch
        aria-label={schedule.enabled ? "Disable" : "Enable"}
        checked={schedule.enabled}
        disabled={update.isPending}
        onCheckedChange={(checked) =>
          update.mutate({ id: schedule.id, patch: { enabled: checked } })
        }
      />
      <Button
        type="button"
        variant="ghost"
        size="icon"
        aria-label="Delete schedule"
        onClick={() => remove.mutate(schedule.id)}
        disabled={remove.isPending}
      >
        <TrashIcon className="size-4" />
      </Button>
    </li>
  );
}

export function SchedulesPanel({ conversationId }: { conversationId: string }) {
  const { data: schedules, isLoading, isError } = useSchedules(conversationId);

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-auto p-3">
      {isLoading && <p className="text-sm text-muted-foreground">Loading schedules…</p>}
      {isError && <p className="text-sm text-destructive">Failed to load schedules.</p>}
      {schedules && schedules.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No loops or monitors yet. The agent can add them with the <code>create_loop</code> /{" "}
          <code>create_monitor</code> tools.
        </p>
      )}
      {schedules && schedules.length > 0 && (
        <ul className="flex flex-col gap-2">
          {schedules.map((s) => (
            <ScheduleRow key={s.id} schedule={s} />
          ))}
        </ul>
      )}
    </div>
  );
}

export default SchedulesPanel;
