/**
 * Schedules page (`/schedules`) — every cron loop across the workspace (#6).
 *
 * Loops are either **global** (spawn a fresh run for a registered agent on each
 * fire) or **conversation-scoped** (fire into that conversation). This is the
 * global management surface; the per-conversation right-rail tab shows only the
 * open conversation's loops. Agents create loops with the `create_loop` tool.
 */

import { AlertTriangleIcon, RepeatIcon, TrashIcon } from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Link } from "@/lib/routing";
import type { Schedule } from "@/lib/schedulesApi";
import {
  useAllSchedules,
  useDeleteSchedule,
  useOnlineRunners,
  useUpdateSchedule,
} from "@/hooks/useSchedules";

function ScheduleRow({ schedule }: { schedule: Schedule }) {
  const update = useUpdateSchedule();
  const remove = useDeleteSchedule();

  return (
    <li className="flex items-center gap-3 rounded-lg border px-3 py-2" data-testid="schedule-row">
      <RepeatIcon className="size-4 shrink-0 text-muted-foreground" />
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium">{schedule.name}</div>
        <div className="truncate font-mono text-xs text-muted-foreground">{schedule.cron}</div>
      </div>
      {/* Target: a registered agent (global loop) or the owning conversation. */}
      <span className="shrink-0 text-xs text-muted-foreground">
        {schedule.agent_name ? (
          <>
            agent · <span className="font-medium">{schedule.agent_name}</span>
          </>
        ) : schedule.conversation_id ? (
          <Link className="underline" to={`/c/${schedule.conversation_id}`}>
            in conversation
          </Link>
        ) : null}
      </span>
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

export function SchedulesPage() {
  const { data: schedules, isLoading, isError } = useAllSchedules();
  const { data: runners } = useOnlineRunners();
  // Loops only for now; monitors ship as a follow-up (host-side streaming).
  const loops = (schedules ?? []).filter((s) => s.kind === "loop");
  const hasGlobalLoops = loops.some((s) => s.agent_name);
  // Warn only once the runners query has resolved (avoid a flash while loading).
  const noRunnerOnline = runners !== undefined && runners.length === 0;

  return (
    <PageScroll contentClassName="px-6">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Schedules</h1>
        {loops.length > 0 && (
          <span className="text-sm text-muted-foreground">
            {loops.length === 1 ? "1 loop" : `${loops.length} loops`}
          </span>
        )}
      </div>

      {hasGlobalLoops && noRunnerOnline && (
        <div
          data-testid="no-runner-warning"
          className="mb-4 flex items-center gap-2 rounded-lg border border-warning/30 bg-warning/5 px-3 py-2 text-sm"
        >
          <AlertTriangleIcon className="size-4 shrink-0 text-warning" />
          <span>
            No runner is connected — <strong>global loops are paused</strong>. A fresh run needs a
            live host to execute on; they resume automatically when a runner connects.
          </span>
        </div>
      )}

      {isLoading && <p className="text-sm text-muted-foreground">Loading schedules…</p>}
      {isError && <p className="text-sm text-destructive">Failed to load schedules.</p>}

      {!isLoading && !isError && loops.length === 0 && (
        <div className="flex flex-col items-center gap-2 py-16 text-center">
          <RepeatIcon className="size-8 text-muted-foreground/50" />
          <p className="text-sm font-medium">No loops scheduled</p>
          <p className="text-xs text-muted-foreground">
            Agents create loops with the <code>create_loop</code> tool — pass an <code>agent</code>{" "}
            for a global loop that spawns a fresh run each fire.
          </p>
        </div>
      )}

      {loops.length > 0 && (
        <ul className="flex flex-col gap-2">
          {loops.map((s) => (
            <ScheduleRow key={s.id} schedule={s} />
          ))}
        </ul>
      )}
    </PageScroll>
  );
}

export default SchedulesPage;
