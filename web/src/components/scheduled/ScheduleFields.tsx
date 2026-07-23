// The schedule-builder sub-form used by the manual create dialog.
//
// Top-level frequency options are Hourly, Daily, Weekdays, and Weekly. Each is a simple
// preset: Hourly takes no inputs, Daily/Weekdays take a time, Weekly adds a
// weekday multi-select. Emits its state up as a ScheduleModel; the parent turns
// it into an RRULE via buildRRule and gates submit on validateSchedule.
//
// TODO: restore the "Custom" entry point when product supports interval-based
// Monthly/Yearly schedules. Its model fields, buildRRule
// cases, and scheduleText/nextRun handling for INTERVAL / BYMONTH /
// multi-BYMONTHDAY / yearly are intentionally KEPT in the lib files
// (scheduleBuilder.ts, scheduleText.ts) so they stay robust; they are not
// reachable from this form today.

import { Label } from "@/components/scheduled/Label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";
import {
  WEEKDAY_CODES,
  validateSchedule,
  type ScheduleModel,
  type SchedulePreset,
  type WeekdayCode,
} from "@/lib/scheduleBuilder";
import { formatClockTime } from "@/lib/scheduleText";

// Presets only: "custom" is deferred (see file header) and is
// deliberately absent from this list, so it's unreachable from the dropdown.
const PRESET_OPTIONS: { value: SchedulePreset; label: string }[] = [
  { value: "hourly", label: "Hourly" },
  { value: "daily", label: "Daily" },
  { value: "weekdays", label: "Weekdays" },
  { value: "weekly", label: "Weekly" },
];

/** Time-of-day is constrained to 15-minute slots (no free typing). */
const MINUTE_SLOTS = [0, 15, 30, 45] as const;

/** Every 15-minute slot across the day (96 options), for the Time dropdown. */
const TIME_SLOTS: { hour: number; minute: number }[] = Array.from({ length: 96 }, (_, i) => ({
  hour: Math.floor(i / 4),
  minute: (i % 4) * 15,
}));

const WEEKDAY_LABELS: Record<WeekdayCode, string> = {
  MO: "Mon",
  TU: "Tue",
  WE: "Wed",
  TH: "Thu",
  FR: "Fri",
  SA: "Sat",
  SU: "Sun",
};

export function ScheduleFields({
  model,
  onChange,
  onSelectOpenChange,
}: {
  model: ScheduleModel;
  onChange: (next: ScheduleModel) => void;
  /** Forwarded to the frequency Select's onOpenChange so the parent Dialog can
   * keep an open Select from dismissing the whole modal. Optional. */
  onSelectOpenChange?: (open: boolean) => void;
}) {
  // Time-of-day is meaningless for the hourly preset (fires every hour); it
  // shows a minute-only input instead.
  const isHourly = model.preset === "hourly";
  const showWeekdays = model.preset === "weekly";

  const error = validateSchedule(model);

  function toggleWeekday(code: WeekdayCode) {
    const has = model.weekdays.includes(code);
    const next = has ? model.weekdays.filter((c) => c !== code) : [...model.weekdays, code];
    onChange({ ...model, weekdays: next });
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="schedule-preset">Frequency</Label>
        <Select
          value={model.preset}
          onValueChange={(value) => onChange({ ...model, preset: value as SchedulePreset })}
          onOpenChange={onSelectOpenChange}
        >
          <SelectTrigger id="schedule-preset" data-testid="schedule-preset-trigger">
            <SelectValue />
          </SelectTrigger>
          {/* position="popper" opens the list anchored BELOW the trigger (auto-
              flips up when no room) so it never overlaps the field label above,
              unlike the default item-aligned mode. align="start" lines the
              dropdown's left edge up with the trigger (Radix defaults to
              center, which shifts it left). */}
          <SelectContent position="popper" align="start">
            {PRESET_OPTIONS.map((opt) => (
              <SelectItem key={opt.value} value={opt.value}>
                {opt.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {showWeekdays && (
        <div className="flex flex-col gap-1.5">
          <Label>On days</Label>
          <div className="flex flex-wrap gap-1.5" role="group" aria-label="Weekdays">
            {WEEKDAY_CODES.map((code) => {
              const selected = model.weekdays.includes(code);
              return (
                <button
                  key={code}
                  type="button"
                  aria-pressed={selected}
                  data-testid={`weekday-${code}`}
                  onClick={() => toggleWeekday(code)}
                  className={cn(
                    "h-8 min-w-11 rounded-md border px-2 text-xs font-medium transition-colors",
                    selected
                      ? "border-primary bg-primary text-primary-foreground"
                      : "border-border bg-background text-muted-foreground hover:bg-muted",
                  )}
                >
                  {WEEKDAY_LABELS[code]}
                </button>
              );
            })}
          </div>
        </div>
      )}

      <div className="flex flex-col gap-1.5">
        <Label htmlFor="schedule-time">{isHourly ? "Minute" : "Time"}</Label>
        {isHourly ? (
          // Hourly fires every hour; only the minute-of-hour matters. Constrain
          // to 15-min slots (0/15/30/45) via a dropdown — no free typing.
          <Select
            value={String(snapMinute(model.minute))}
            onValueChange={(v) => onChange({ ...model, minute: Number(v) })}
            onOpenChange={onSelectOpenChange}
          >
            <SelectTrigger id="schedule-time" data-testid="schedule-minute" className="w-28">
              <SelectValue />
            </SelectTrigger>
            <SelectContent position="popper" align="start">
              {MINUTE_SLOTS.map((m) => (
                <SelectItem key={m} value={String(m)}>
                  {`:${pad(m)}`}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : (
          // Time-of-day constrained to 15-min slots (96/day) via a dropdown.
          // Value encodes "H:M"; labels use the same 12h clock as the list rows.
          <Select
            value={`${snapHour(model)}:${snapMinute(model.minute)}`}
            onValueChange={(v) => {
              const [h, m] = v.split(":");
              onChange({ ...model, hour: Number(h), minute: Number(m) });
            }}
            onOpenChange={(next) => {
              onSelectOpenChange?.(next);
              // Radix scrolls the SELECTED slot into view on open (even in
              // popper mode), so a 9:00 AM default would land mid-list and hide
              // 12:00 AM. Pin the viewport to the top so the list always starts
              // at 12:00 AM (index 0), per product. Radix's scroll-into-view
              // runs in a layout effect + a follow-up frame after open, so we
              // pin on a short interval for a few ticks to reliably win the
              // race regardless of machine speed, then stop.
              if (next) {
                let ticks = 0;
                const pinTop = () => {
                  document
                    .querySelectorAll<HTMLElement>("[data-radix-select-viewport]")
                    .forEach((vp) => {
                      vp.scrollTop = 0;
                    });
                };
                const timer = setInterval(() => {
                  pinTop();
                  if (++ticks >= 6) clearInterval(timer);
                }, 16);
                pinTop();
              }
            }}
          >
            <SelectTrigger id="schedule-time" data-testid="schedule-time" className="w-40">
              <SelectValue />
            </SelectTrigger>
            {/* position="popper" + align="start" opens the list below the
                trigger, left-aligned. We also reset the viewport scroll to top
                on open (see onOpenChange) so the list starts at 12:00 AM (index
                0) rather than scrolled to the selected slot. max-h ≈ 12 rows
                (~28px each) so the 96 slots scroll inside the popover. */}
            <SelectContent position="popper" align="start" className="max-h-[21rem]">
              {TIME_SLOTS.map((slot) => (
                <SelectItem
                  key={`${slot.hour}:${slot.minute}`}
                  value={`${slot.hour}:${slot.minute}`}
                >
                  {formatClockTime(slot.hour, slot.minute)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
      </div>

      {/* describeSchedule/buildRRule stay in the lib for list rows and possible
          future previews; only the inline validation error renders here now. */}
      {error && (
        <p className="text-xs text-destructive" data-testid="schedule-error">
          {error}
        </p>
      )}
    </div>
  );
}

/** Zero-pad a number to two digits (minute labels, e.g. ":05"). */
function pad(n: number): string {
  return n.toString().padStart(2, "0");
}

/**
 * Snap a stored minute onto the nearest 15-min slot (0/15/30/45) so the Select's
 * controlled value always matches one of its options — otherwise a model minute
 * left off-grid (e.g. from the default 0, or a future preset) would show a blank
 * trigger. Rounds to nearest; 53 → 45, 8 → 15.
 */
function snapMinute(n: number): number {
  if (!Number.isFinite(n)) return 0;
  const slot = Math.round(Math.min(59, Math.max(0, n)) / 15) * 15;
  return slot === 60 ? 45 : slot;
}

/** The stored hour clamped to 0–23 for the Time dropdown's value. */
function snapHour(model: ScheduleModel): number {
  const h = model.hour;
  if (!Number.isFinite(h)) return 0;
  return Math.min(23, Math.max(0, Math.trunc(h)));
}
