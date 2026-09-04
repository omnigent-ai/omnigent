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

import { useEffect, useRef, useState, type ReactNode } from "react";
import { ClockIcon } from "lucide-react";
import { Label } from "@/components/scheduled/Label";
import { Input } from "@/components/ui/input";
import { Popover, PopoverAnchor, PopoverContent } from "@/components/ui/popover";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";
import {
  DEFAULT_SCHEDULE_MODEL,
  WEEKDAY_CODES,
  formatTimeOfDay24,
  parseMinuteOfHourInput,
  parseTimeOfDayInput,
  validateSchedule,
  type ScheduleModel,
  type SchedulePreset,
  type WeekdayCode,
} from "@/lib/scheduleBuilder";

// Presets only: "custom" is deferred (see file header) and is
// deliberately absent from this list, so it's unreachable from the dropdown.
const PRESET_OPTIONS: { value: SchedulePreset; label: string }[] = [
  { value: "hourly", label: "Hourly" },
  { value: "daily", label: "Daily" },
  { value: "weekdays", label: "Weekdays" },
  { value: "weekly", label: "Weekly" },
];

const HOURS_12 = Array.from({ length: 12 }, (_, i) => i + 1);
const MINUTES = Array.from({ length: 60 }, (_, i) => i);
const PERIODS = ["AM", "PM"] as const;
type Period = (typeof PERIODS)[number];

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
  // The active-range control only makes sense for an hourly cadence. Custom is
  // unreachable from this form's dropdown today (see file header), but the
  // check stays here for parity with buildRRule/validateSchedule, which also
  // keep the Custom paths alive.
  const showActiveRange = isHourly || (model.preset === "custom" && model.customFreq === "hourly");

  const error = validateSchedule(model);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const hourColumnRef = useRef<HTMLDivElement | null>(null);
  const minuteColumnRef = useRef<HTMLDivElement | null>(null);
  const periodColumnRef = useRef<HTMLDivElement | null>(null);
  const [timeText, setTimeText] = useState(() =>
    formatInputValue(model.hour, model.minute, isHourly),
  );
  const [timePickerOpen, setTimePickerOpen] = useState(false);

  useEffect(() => {
    if (document.activeElement === inputRef.current) return;
    setTimeText(formatInputValue(model.hour, model.minute, isHourly));
  }, [isHourly, model.hour, model.minute]);

  const pickerParts = toPickerParts(getPickerTime());

  useEffect(() => {
    if (!timePickerOpen || isHourly) return;
    scrollSelectedIntoView(hourColumnRef.current);
    scrollSelectedIntoView(minuteColumnRef.current);
  }, [isHourly, pickerParts.hour12, pickerParts.minute, timePickerOpen]);

  useEffect(() => {
    if (isHourly) return;
    const handleWheel = (event: WheelEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      const columns = [
        hourColumnRef.current ?? document.querySelector('[data-testid="schedule-hour-column"]'),
        minuteColumnRef.current ?? document.querySelector('[data-testid="schedule-minute-column"]'),
        periodColumnRef.current ?? document.querySelector('[data-testid="schedule-period-column"]'),
      ];
      const column = columns.find((col) => col?.contains(target));
      if (!column) return;
      if (column.scrollHeight > column.clientHeight) {
        column.scrollTop += event.deltaY;
      }
      event.preventDefault();
      event.stopImmediatePropagation();
      event.stopPropagation();
    };
    window.addEventListener("wheel", handleWheel, { capture: true, passive: false });
    return () => {
      window.removeEventListener("wheel", handleWheel, true);
    };
  }, [isHourly]);

  function toggleWeekday(code: WeekdayCode) {
    const has = model.weekdays.includes(code);
    const next = has ? model.weekdays.filter((c) => c !== code) : [...model.weekdays, code];
    onChange({ ...model, weekdays: next });
  }

  function handleTimeTextChange(value: string) {
    if (isHourly) {
      const digits = value.replace(/\D/g, "").slice(0, 2);
      let minute = digits === "" ? Number.NaN : Number(digits);
      if (Number.isInteger(minute) && minute > 59) minute = 59;
      const text = Number.isInteger(minute) ? String(minute) : "";
      setTimeText(text);
      onChange({ ...model, minute: Number.isInteger(minute) ? minute : Number.NaN });
      return;
    }

    setTimeText(value);
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
    if (parsed !== null) setTimeText(formatTimeInput(parsed.hour, parsed.minute));
  }

  function handleTimePickerOpenChange(next: boolean) {
    setTimePickerOpen(next);
    onSelectOpenChange?.(next);
  }

  function getPickerTime(): { hour: number; minute: number } {
    const parsed = parseTimeOfDayInput(timeText);
    if (parsed !== null) return parsed;
    if (Number.isInteger(model.hour) && Number.isInteger(model.minute)) {
      return { hour: model.hour, minute: model.minute };
    }
    return { hour: DEFAULT_SCHEDULE_MODEL.hour, minute: DEFAULT_SCHEDULE_MODEL.minute };
  }

  function applyPickerTime(next: Partial<{ hour12: number; minute: number; period: Period }>) {
    const current = toPickerParts(getPickerTime());
    const hour12 = next.hour12 ?? current.hour12;
    const minute = next.minute ?? current.minute;
    const period = next.period ?? current.period;
    const hour = to24Hour(hour12, period);
    setTimeText(formatTimeInput(hour, minute));
    onChange({ ...model, hour, minute });
  }

  function handlePresetChange(preset: SchedulePreset) {
    const staysHourly = preset === "hourly" || (preset === "custom" && model.customFreq === "hourly");
    onChange({ ...model, preset, activeRange: staysHourly ? model.activeRange : null });
  }

  function handleActiveRangeToggle(enabled: boolean) {
    onChange({
      ...model,
      activeRange: enabled ? { start: formatTimeOfDay24(9, 0), end: formatTimeOfDay24(17, 0) } : null,
    });
  }

  function handleActiveRangeStartChange(hhmm: string) {
    if (!model.activeRange) return;
    onChange({ ...model, activeRange: { ...model.activeRange, start: hhmm } });
  }

  function handleActiveRangeEndChange(hhmm: string) {
    if (!model.activeRange) return;
    onChange({ ...model, activeRange: { ...model.activeRange, end: hhmm } });
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="grid gap-3 sm:grid-cols-2 sm:gap-6" data-testid="schedule-frequency-time-row">
        <div
          className="flex w-full min-w-0 flex-col gap-1.5"
          data-testid="schedule-frequency-control"
        >
          <Label htmlFor="schedule-preset">Frequency</Label>
          <Select
            value={model.preset}
            componentId="tasks.scheduled.schedule_frequency"
            valueHasNoPii
            onValueChange={(value) => handlePresetChange(value as SchedulePreset)}
            onOpenChange={onSelectOpenChange}
          >
            <SelectTrigger
              id="schedule-preset"
              data-testid="schedule-preset-trigger"
              className="w-full"
            >
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

        <div className="flex w-full min-w-0 flex-col gap-1.5" data-testid="schedule-time-control">
          <Label htmlFor="schedule-time">{isHourly ? "Minute" : "Time"}</Label>
          {isHourly ? (
            <Input
              ref={inputRef}
              id="schedule-time"
              value={timeText}
              data-testid="schedule-minute"
              placeholder="0"
              className="text-ui"
              aria-invalid={error ? true : undefined}
              onChange={(e) => handleTimeTextChange(e.target.value)}
              onBlur={canonicalizeTimeText}
            />
          ) : (
            <Popover open={timePickerOpen} onOpenChange={handleTimePickerOpenChange}>
              <PopoverAnchor asChild>
                <div className="relative">
                  <Input
                    ref={inputRef}
                    id="schedule-time"
                    value={timeText}
                    data-testid="schedule-time"
                    placeholder="5:00 PM"
                    className="pr-8 text-ui"
                    aria-invalid={error ? true : undefined}
                    onFocus={() => handleTimePickerOpenChange(true)}
                    onChange={(e) => handleTimeTextChange(e.target.value)}
                    onBlur={canonicalizeTimeText}
                  />
                  <button
                    type="button"
                    aria-label="Open time picker"
                    data-testid="schedule-time-picker-trigger"
                    className="absolute top-1/2 right-2 flex size-4 -translate-y-1/2 items-center justify-center rounded-sm text-muted-foreground hover:text-foreground"
                    onClick={() => handleTimePickerOpenChange(!timePickerOpen)}
                  >
                    <ClockIcon className="size-3.5" />
                  </button>
                </div>
              </PopoverAnchor>
              <PopoverContent
                align="start"
                className="w-64 rounded-sm p-1.5"
                onOpenAutoFocus={(event) => event.preventDefault()}
              >
                <div className="grid grid-cols-3 gap-1" data-testid="schedule-time-picker">
                  <div
                    ref={hourColumnRef}
                    className="max-h-40 overflow-y-auto overscroll-contain pr-0.5"
                    data-testid="schedule-hour-column"
                  >
                    {HOURS_12.map((hour) => (
                      <PickerCell
                        key={hour}
                        testId={`schedule-hour-${pad(hour)}`}
                        selected={pickerParts.hour12 === hour}
                        onClick={() => applyPickerTime({ hour12: hour })}
                      >
                        {pad(hour)}
                      </PickerCell>
                    ))}
                  </div>
                  <div
                    ref={minuteColumnRef}
                    className="max-h-40 overflow-y-auto overscroll-contain pr-0.5"
                    data-testid="schedule-minute-column"
                  >
                    {MINUTES.map((minute) => (
                      <PickerCell
                        key={minute}
                        testId={`schedule-minute-${pad(minute)}`}
                        selected={pickerParts.minute === minute}
                        onClick={() => applyPickerTime({ minute })}
                      >
                        {pad(minute)}
                      </PickerCell>
                    ))}
                  </div>
                  <div
                    ref={periodColumnRef}
                    className="max-h-40 overflow-y-auto overscroll-contain pr-0.5"
                    data-testid="schedule-period-column"
                  >
                    {PERIODS.map((period) => (
                      <PickerCell
                        key={period}
                        testId={`schedule-period-${period}`}
                        selected={pickerParts.period === period}
                        onClick={() => applyPickerTime({ period })}
                      >
                        {period}
                      </PickerCell>
                    ))}
                  </div>
                </div>
              </PopoverContent>
            </Popover>
          )}
        </div>
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
                    "h-8 min-w-11 rounded-md border px-2 text-sm font-medium transition-colors",
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

      {showActiveRange && (
        <div className="flex flex-col gap-1.5" data-testid="schedule-active-range-field">
          <div className="flex items-center gap-2">
            <Switch
              checked={model.activeRange !== null}
              onCheckedChange={handleActiveRangeToggle}
              aria-label="Only run between"
              data-testid="schedule-active-range-toggle"
              componentId="tasks.scheduled.active_range_toggle"
            />
            <Label>Only run between</Label>
          </div>
          {model.activeRange && (
            <div className="grid grid-cols-2 gap-3" data-testid="schedule-active-range-times">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="schedule-active-range-start-input">Start</Label>
                <TimeOfDayPicker
                  id="schedule-active-range-start-input"
                  value={model.activeRange.start}
                  onChange={handleActiveRangeStartChange}
                  placeholder="9:00 AM"
                  testIdPrefix="schedule-active-range-start"
                  onSelectOpenChange={onSelectOpenChange}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="schedule-active-range-end-input">End</Label>
                <TimeOfDayPicker
                  id="schedule-active-range-end-input"
                  value={model.activeRange.end}
                  onChange={handleActiveRangeEndChange}
                  placeholder="5:00 PM"
                  testIdPrefix="schedule-active-range-end"
                  onSelectOpenChange={onSelectOpenChange}
                />
              </div>
            </div>
          )}
        </div>
      )}

      {/* describeSchedule/buildRRule stay in the lib for list rows and possible
          future previews; only the inline validation error renders here now. */}
      {error && (
        <p className="text-sm text-destructive" data-testid="schedule-error">
          {error}
        </p>
      )}
    </div>
  );
}

function pad(n: number): string {
  return n.toString().padStart(2, "0");
}

function formatInputValue(hour: number, minute: number, isHourly: boolean): string {
  if (isHourly) return formatMinuteInput(minute);
  if (!Number.isInteger(hour) || !Number.isInteger(minute)) return "";
  return formatTimeInput(hour, minute);
}

function formatMinuteInput(minute: number): string {
  if (!Number.isInteger(minute)) return "";
  return String(Math.min(59, Math.max(0, minute)));
}

function formatTimeInput(hour: number, minute: number): string {
  if (!Number.isInteger(hour) || !Number.isInteger(minute)) return "";
  const parts = toPickerParts({ hour, minute });
  return `${pad(parts.hour12)}:${pad(parts.minute)} ${parts.period}`;
}

function toPickerParts(time: { hour: number; minute: number }): {
  hour12: number;
  minute: number;
  period: Period;
} {
  const hour = Math.min(23, Math.max(0, time.hour));
  return {
    hour12: hour % 12 === 0 ? 12 : hour % 12,
    minute: Math.min(59, Math.max(0, time.minute)),
    period: hour >= 12 ? "PM" : "AM",
  };
}

function to24Hour(hour12: number, period: Period): number {
  return (hour12 % 12) + (period === "PM" ? 12 : 0);
}

function scrollSelectedIntoView(column: HTMLDivElement | null) {
  column
    ?.querySelector<HTMLElement>('[data-selected="true"]')
    ?.scrollIntoView?.({ block: "center" });
}

function PickerCell({
  children,
  onClick,
  selected,
  testId,
}: {
  children: ReactNode;
  onClick: () => void;
  selected: boolean;
  testId: string;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      data-selected={selected ? "true" : undefined}
      data-testid={testId}
      className={cn(
        "flex h-8 w-full items-center justify-center rounded-sm text-ui transition-colors",
        selected ? "bg-primary text-primary-foreground" : "text-foreground hover:bg-muted",
      )}
      onClick={onClick}
    >
      {children}
    </button>
  );
}

/**
 * A single time-of-day input + Popover picker, mirroring the `schedule-time`
 * control above (hour/minute/period columns via `PickerCell`) but driven by a
 * standalone strict 24-hour `"HH:MM"` string rather than the RRULE's
 * hour/minute fields — used for the active-range start/end bounds, which are
 * a sibling of the rule, not part of it.
 */
function TimeOfDayPicker({
  id,
  value,
  onChange,
  placeholder,
  testIdPrefix,
  onSelectOpenChange,
}: {
  id?: string;
  /** Strict 24-hour `"HH:MM"`, e.g. `"09:00"`. */
  value: string;
  onChange: (hhmm: string) => void;
  placeholder: string;
  testIdPrefix: string;
  onSelectOpenChange?: (open: boolean) => void;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const hourColumnRef = useRef<HTMLDivElement | null>(null);
  const minuteColumnRef = useRef<HTMLDivElement | null>(null);
  const periodColumnRef = useRef<HTMLDivElement | null>(null);
  const [text, setText] = useState(() => formatValueForDisplay(value));
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (document.activeElement === inputRef.current) return;
    setText(formatValueForDisplay(value));
  }, [value]);

  const parsedText = parseTimeOfDayInput(text);
  const parsedValue = parseTimeOfDayInput(value);
  const pickerTime = parsedText ?? parsedValue ?? { hour: 9, minute: 0 };
  const pickerParts = toPickerParts(pickerTime);

  useEffect(() => {
    if (!open) return;
    scrollSelectedIntoView(hourColumnRef.current);
    scrollSelectedIntoView(minuteColumnRef.current);
  }, [open, pickerParts.hour12, pickerParts.minute]);

  function handleOpenChange(next: boolean) {
    setOpen(next);
    onSelectOpenChange?.(next);
  }

  function handleTextChange(next: string) {
    setText(next);
    const parsed = parseTimeOfDayInput(next);
    if (parsed !== null) onChange(formatTimeOfDay24(parsed.hour, parsed.minute));
  }

  function canonicalize() {
    const parsed = parseTimeOfDayInput(text);
    if (parsed !== null) setText(formatTimeInput(parsed.hour, parsed.minute));
  }

  function applyPickerTime(next: Partial<{ hour12: number; minute: number; period: Period }>) {
    const hour12 = next.hour12 ?? pickerParts.hour12;
    const minute = next.minute ?? pickerParts.minute;
    const period = next.period ?? pickerParts.period;
    const hour = to24Hour(hour12, period);
    setText(formatTimeInput(hour, minute));
    onChange(formatTimeOfDay24(hour, minute));
  }

  return (
    <Popover open={open} onOpenChange={handleOpenChange}>
      <PopoverAnchor asChild>
        <div className="relative">
          <Input
            ref={inputRef}
            id={id}
            value={text}
            data-testid={`${testIdPrefix}-input`}
            placeholder={placeholder}
            className="pr-8 text-ui"
            onFocus={() => handleOpenChange(true)}
            onChange={(e) => handleTextChange(e.target.value)}
            onBlur={canonicalize}
          />
          <button
            type="button"
            aria-label="Open time picker"
            data-testid={`${testIdPrefix}-picker-trigger`}
            className="absolute top-1/2 right-2 flex size-4 -translate-y-1/2 items-center justify-center rounded-sm text-muted-foreground hover:text-foreground"
            onClick={() => handleOpenChange(!open)}
          >
            <ClockIcon className="size-3.5" />
          </button>
        </div>
      </PopoverAnchor>
      <PopoverContent
        align="start"
        className="w-64 rounded-sm p-1.5"
        onOpenAutoFocus={(event) => event.preventDefault()}
      >
        <div className="grid grid-cols-3 gap-1" data-testid={`${testIdPrefix}-picker`}>
          <div
            ref={hourColumnRef}
            className="max-h-40 overflow-y-auto overscroll-contain pr-0.5"
            data-testid={`${testIdPrefix}-hour-column`}
          >
            {HOURS_12.map((hour) => (
              <PickerCell
                key={hour}
                testId={`${testIdPrefix}-hour-${pad(hour)}`}
                selected={pickerParts.hour12 === hour}
                onClick={() => applyPickerTime({ hour12: hour })}
              >
                {pad(hour)}
              </PickerCell>
            ))}
          </div>
          <div
            ref={minuteColumnRef}
            className="max-h-40 overflow-y-auto overscroll-contain pr-0.5"
            data-testid={`${testIdPrefix}-minute-column`}
          >
            {MINUTES.map((minute) => (
              <PickerCell
                key={minute}
                testId={`${testIdPrefix}-minute-${pad(minute)}`}
                selected={pickerParts.minute === minute}
                onClick={() => applyPickerTime({ minute })}
              >
                {pad(minute)}
              </PickerCell>
            ))}
          </div>
          <div
            ref={periodColumnRef}
            className="max-h-40 overflow-y-auto overscroll-contain pr-0.5"
            data-testid={`${testIdPrefix}-period-column`}
          >
            {PERIODS.map((period) => (
              <PickerCell
                key={period}
                testId={`${testIdPrefix}-period-${period}`}
                selected={pickerParts.period === period}
                onClick={() => applyPickerTime({ period })}
              >
                {period}
              </PickerCell>
            ))}
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}

function formatValueForDisplay(value: string): string {
  const parsed = parseTimeOfDayInput(value);
  return parsed ? formatTimeInput(parsed.hour, parsed.minute) : "";
}
