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

import { useEffect, useRef, useState } from "react";
import { Label } from "@/components/scheduled/Label";
import { Input } from "@/components/ui/input";
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
  parseMinuteOfHourInput,
  parseTimeOfDayInput,
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
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [timeText, setTimeText] = useState(() => formatInputValue(model, isHourly));

  useEffect(() => {
    if (document.activeElement === inputRef.current) return;
    setTimeText(formatInputValue(model, isHourly));
  }, [isHourly, model.hour, model.minute]);

  function toggleWeekday(code: WeekdayCode) {
    const has = model.weekdays.includes(code);
    const next = has ? model.weekdays.filter((c) => c !== code) : [...model.weekdays, code];
    onChange({ ...model, weekdays: next });
  }

  function handleTimeTextChange(value: string) {
    setTimeText(value);
    if (isHourly) {
      const minute = parseMinuteOfHourInput(value);
      onChange({ ...model, minute: minute ?? Number.NaN });
      return;
    }

    const parsed = parseTimeOfDayInput(value);
    onChange({
      ...model,
      hour: parsed?.hour ?? Number.NaN,
      minute: parsed?.minute ?? Number.NaN,
    });
  }

  function canonicalizeTimeText() {
    if (isHourly) {
      const minute = parseMinuteOfHourInput(timeText);
      if (minute !== null) setTimeText(formatMinuteInput(minute));
      return;
    }

    const parsed = parseTimeOfDayInput(timeText);
    if (parsed !== null) setTimeText(formatClockTime(parsed.hour, parsed.minute));
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
        <Input
          ref={inputRef}
          id="schedule-time"
          value={timeText}
          data-testid={isHourly ? "schedule-minute" : "schedule-time"}
          placeholder={isHourly ? ":15" : "5:00 PM"}
          className={isHourly ? "w-28" : "w-40"}
          aria-invalid={error ? true : undefined}
          onChange={(e) => handleTimeTextChange(e.target.value)}
          onBlur={canonicalizeTimeText}
        />
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

function pad(n: number): string {
  return n.toString().padStart(2, "0");
}

function formatInputValue(model: ScheduleModel, isHourly: boolean): string {
  if (isHourly) return formatMinuteInput(model.minute);
  if (!Number.isInteger(model.hour) || !Number.isInteger(model.minute)) return "";
  return formatClockTime(model.hour, model.minute);
}

function formatMinuteInput(minute: number): string {
  if (!Number.isInteger(minute) || minute < 0 || minute > 59) return "";
  return `:${pad(minute)}`;
}
