// One row in the Tasks list, rendered as a FLAT list row (no card chrome — no
// border/background/shadow box, no per-row divider). Layout: bold task title on
// line 1 (+ a small "Paused" pill when paused), a single muted subline on line 2
// with the human-readable schedule summary ("Weekdays at 8:00 AM"). We show ONLY
// the schedule — no "Next run in Xh" countdown — because a client-computed
// next-run can't be guaranteed to match the server's anchor for INTERVAL>1
// rules; the schedule summary is always correct. Paused rows are NOT dimmed —
// the title stays fully legible and the pill is the sole paused signal. A
// hover-revealed ellipsis (⋯) action menu (Pause/Resume + Delete) sits on the
// right. No leading or trailing status circles — the ⋯ menu is the only affordance.

import { useMemo, useState } from "react";
import { MoreHorizontalIcon, PauseIcon, PlayIcon, Trash2Icon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import { describeSchedule } from "@/lib/scheduleText";
import type { ScheduledTask } from "@/lib/scheduledTasksApi";

export function ScheduledTaskRow({
  task,
  onPauseToggle,
  onDelete,
  busy,
}: {
  task: ScheduledTask;
  onPauseToggle: (task: ScheduledTask) => void;
  onDelete: (task: ScheduledTask) => void;
  busy: boolean;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const paused = task.state === "paused";
  // The subtitle is just the schedule summary for both active and paused rows
  // (no next-run countdown). The paused signal is the pill next to the title,
  // NOT a "· Paused" suffix and NOT row-wide dimming.
  const secondaryLine = useMemo(() => describeSchedule(task.rrule), [task.rrule]);

  return (
    <div
      data-testid="scheduled-task-row"
      data-state={task.state}
      className={cn(
        // Flat row — NO card chrome (no border/bg/shadow box) and no per-row
        // divider. `group relative` so the absolutely-positioned ⋯ trigger can
        // hover-reveal; vertical padding gives the flat-list spacing.
        // `-mx-2 rounded-lg` + `hover:bg-muted/50` gives a subtle FULL-ROW hover
        // highlight (like the sidebar conversation rows) that extends past the
        // content while keeping the title aligned with the page. `pl-2` (left
        // content inset) is mirrored by the ⋯ button's `right-2` inset so the
        // two edges are symmetric within the highlight; `pr-10` keeps the text
        // clear of the inset button. Paused rows are NOT dimmed — the title must
        // stay legible (AA); the "Paused" pill is the sole signal.
        "group relative -mx-2 flex items-center gap-3 rounded-lg py-3 pr-10 pl-2 transition-colors hover:bg-muted/50",
      )}
    >
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate text-sm font-semibold">{task.name}</span>
          {paused && (
            <span
              data-testid="task-paused-pill"
              className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground"
            >
              Paused
            </span>
          )}
        </span>
        <span className="truncate text-xs text-muted-foreground" data-testid="task-schedule-line">
          {secondaryLine}
        </span>
      </div>

      {/* Hover-revealed ellipsis menu, mirroring the sidebar conversation-row
          action button: absolute-positioned on the right, hidden until the row
          is hovered / focused, and kept surfaced while the menu is open via
          `aria-expanded`. */}
      <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={`Actions for ${task.name}`}
            data-testid="task-row-menu"
            disabled={busy}
            className={cn(
              "-translate-y-1/2 absolute top-1/2 right-2 transition-opacity",
              "md:opacity-0 md:group-hover:opacity-100 md:group-has-[:focus-visible]:opacity-100",
              "md:aria-expanded:opacity-100",
            )}
          >
            <MoreHorizontalIcon className="size-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onSelect={() => onPauseToggle(task)} data-testid="task-pause-toggle">
            {paused ? (
              <>
                <PlayIcon className="size-4" />
                Resume
              </>
            ) : (
              <>
                <PauseIcon className="size-4" />
                Pause
              </>
            )}
          </DropdownMenuItem>
          <DropdownMenuItem
            variant="destructive"
            onSelect={() => onDelete(task)}
            data-testid="task-delete"
          >
            <Trash2Icon className="size-4" />
            Delete
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
