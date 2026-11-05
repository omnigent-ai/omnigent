// Inline recurrence controls share validation with the automation's ending settings.
import { ScheduleSelect as Select } from "./ScheduleSelect";
import { pickerTriggerClass } from "./pickerStyles";
import { useId, useMemo, useState, type ReactNode } from "react";
import { CheckIcon, ChevronDownIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Command,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ScheduleTimePicker } from "./ScheduleTimePicker";
import { useScheduleEscape } from "./useScheduleEscape";
import { cn } from "@/lib/utils";
import { commonTimezones, dateInTimezone, endOfDayInTimezone } from "@/lib/timezones";
import {
  DEFAULT_SCHEDULE_MODEL,
  MAX_MINUTE_INTERVAL,
  minimumScheduleInterval,
  MIN_SUBHOUR_MINUTE_INTERVAL,
  formatUntil,
  parseUntil,
  validateSchedule,
  WEEKDAY_CODES,
  type CustomFreq,
  type ScheduleModel,
  type SchedulePreset,
  type WeekdayCode,
} from "@/lib/scheduleBuilder";

const PRESETS = [
  { value: "interval", label: "Interval" },
  { value: "hourly", label: "Every hour" },
  { value: "daily", label: "Every day" },
  { value: "weekdays", label: "Every weekday" },
  { value: "weekly", label: "Every week" },
  { value: "custom", label: "Custom schedule" },
];
const FREQUENCIES = [
  { value: "hourly", label: "Hourly" },
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
  { value: "yearly", label: "Yearly" },
];
const INTERVAL_UNITS: Record<CustomFreq, string> = {
  minutely: "minute",
  hourly: "hour",
  daily: "day",
  weekly: "week",
  monthly: "month",
  yearly: "year",
};
const MONTHS = Array.from({ length: 12 }, (_, month) => ({
  value: String(month + 1),
  label: new Intl.DateTimeFormat("en", { month: "long", timeZone: "UTC" }).format(
    new Date(Date.UTC(2024, month, 1)),
  ),
}));
const WEEKDAY_NAMES: Record<WeekdayCode, string> = {
  MO: "Monday",
  TU: "Tuesday",
  WE: "Wednesday",
  TH: "Thursday",
  FR: "Friday",
  SA: "Saturday",
  SU: "Sunday",
};
const choiceClass = "h-8 w-auto min-w-0 text-ui";
const inlineChoiceClass =
  "max-w-full border-0 bg-transparent text-muted-foreground shadow-none dark:bg-transparent";

export function ScheduleFields({
  model,
  anchoredScheduling,
  sessionMode = "new",
  executionTarget,
  subhourSupported = false,
  onChange,
  timezone,
  onTimezoneChange,
  onSelectOpenChange,
  onRecurrenceSelect,
  recurrenceUnsupported = false,
  allowPastEnd = false,
  children,
}: {
  model: ScheduleModel;
  anchoredScheduling: boolean;
  sessionMode?: "new" | "continue";
  executionTarget?: "connected_host" | "managed_sandbox";
  subhourSupported?: boolean;
  onChange: (next: ScheduleModel) => void;
  timezone: string;
  onTimezoneChange: (timezone: string) => void;
  onSelectOpenChange?: (open: boolean) => void;
  onRecurrenceSelect?: () => void;
  recurrenceUnsupported?: boolean;
  allowPastEnd?: boolean;
  children?: (fields: { recurrenceFields: ReactNode; endingFields: ReactNode }) => ReactNode;
}) {
  const errorId = useId();
  const endingErrorId = useId();
  const minuteSchedulesSupported =
    minimumScheduleInterval("minutely", sessionMode, subhourSupported, executionTarget) ===
    MIN_SUBHOUR_MINUTE_INTERVAL;
  // Both editors save the same minute recurrence; saved rules reopen as Interval.
  const [customMinutes, setCustomMinutes] = useState(false);
  const [timezoneOpen, setTimezoneOpen] = useState(false);
  const zones = useMemo(() => commonTimezones(timezone), [timezone]);
  const [dateInput, setDateInput] = useState<{
    value: string;
    until: string;
    timezone: string;
    error: string | null;
  } | null>(null);
  const activeDateInput =
    model.end?.type === "until" &&
    dateInput?.until === model.end.until &&
    dateInput.timezone === timezone
      ? dateInput
      : null;
  const error = validateSchedule(
    { ...model, end: undefined },
    { sessionMode, subhourSupported, executionTarget },
  );
  const endingError =
    activeDateInput?.error ??
    validateSchedule({ ...DEFAULT_SCHEDULE_MODEL, end: model.end }, { allowPastEnd });
  const until = model.end?.type === "until" ? parseUntil(model.end.until) : null;
  const minutely = model.preset === "custom" && model.customFreq === "minutely";
  const custom = model.preset === "custom" && (!minutely || customMinutes);
  const hourly = model.preset === "hourly" || (custom && model.customFreq === "hourly");
  const weekly = model.preset === "weekly" || (custom && model.customFreq === "weekly");
  const monthDays = custom && (model.customFreq === "monthly" || model.customFreq === "yearly");
  let selectedFrequency: string = model.preset;
  if (minutely && !custom) selectedFrequency = "interval";
  if (recurrenceUnsupported) selectedFrequency = "";

  function changePreset(value: string) {
    setCustomMinutes(value === "custom");
    const next: ScheduleModel = {
      ...model,
      preset: value === "interval" ? "custom" : (value as SchedulePreset),
    };
    if (value === "hourly" && !Number.isInteger(next.minute)) next.minute = 0;
    if (value === "interval") {
      next.customFreq = "minutely";
      if (model.customFreq !== "minutely")
        next.interval = minimumScheduleInterval(
          "minutely",
          sessionMode,
          subhourSupported,
          executionTarget,
        );
    } else if (value === "custom") {
      if (model.preset === "weekdays") next.customFreq = "weekly";
      else if (model.preset !== "custom") next.customFreq = model.preset;
      if (model.preset === "weekdays") next.weekdays = ["MO", "TU", "WE", "TH", "FR"];
      if (model.preset !== "custom") next.interval = 1;
    }
    onChange(next);
    onRecurrenceSelect?.();
  }

  function changeCustomFrequency(value: string) {
    setCustomMinutes(true);
    const next = { ...model, customFreq: value as CustomFreq };
    if (value === "minutely") {
      const minimum = minimumScheduleInterval(
        next.customFreq,
        sessionMode,
        subhourSupported,
        executionTarget,
      );
      next.interval = Math.max(minimum, next.interval || minimum);
    } else if (model.customFreq === "minutely") next.interval = 1;
    if (value === "hourly" && !Number.isInteger(next.minute)) next.minute = 0;
    onChange(next);
  }

  function changeEnd(value: string) {
    setDateInput(null);
    let end: ScheduleModel["end"];
    if (value === "count") end = { type: "count", count: 2 };
    if (value === "until") end = { type: "until", until: "" };
    onChange({ ...model, end });
  }

  function changeEndDate(value: string, zone: string) {
    const cutoff = endOfDayInTimezone(value, zone);
    const serialized = cutoff ? formatUntil(cutoff) : "";
    const representable = parseUntil(serialized) !== null;
    const nextUntil = representable ? serialized : "";
    setDateInput({
      value,
      until: nextUntil,
      timezone: zone,
      error:
        value && !representable
          ? "This end date is unavailable in this timezone. Choose another date."
          : null,
    });
    onChange({ ...model, end: { type: "until", until: nextUntil } });
  }

  function changeTimezone(next: string) {
    if (model.end?.type === "until")
      changeEndDate(activeDateInput?.value ?? (until ? dateInTimezone(until, timezone) : ""), next);
    onTimezoneChange(next);
    setTimezoneOpen(false);
    onSelectOpenChange?.(false);
  }

  const intervalInput = (
    <Input
      aria-label="Repeat every"
      data-testid="custom-interval"
      type="number"
      min={minimumScheduleInterval(
        model.customFreq,
        sessionMode,
        subhourSupported,
        executionTarget,
      )}
      max={minutely ? MAX_MINUTE_INTERVAL : 366}
      step={1}
      disabled={!anchoredScheduling}
      value={Number.isFinite(model.interval) ? model.interval : ""}
      className="h-8 w-16 px-2 text-ui focus-visible:ring-0"
      onChange={(event) =>
        onChange({
          ...model,
          interval: event.target.value === "" ? NaN : Number(event.target.value),
        })
      }
    />
  );
  const timezonePicker = (
    <Popover
      modal
      open={timezoneOpen}
      onOpenChange={(open) => {
        setTimezoneOpen(open);
        onSelectOpenChange?.(open);
      }}
    >
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          className={cn(
            "h-8 max-w-full min-w-0 shrink justify-between gap-2 border-0 bg-transparent px-0 font-normal text-muted-foreground shadow-none dark:bg-transparent",
            pickerTriggerClass,
          )}
          role="combobox"
          aria-label="Timezone"
          aria-expanded={timezoneOpen}
          data-testid="task-timezone-trigger"
        >
          <span className="truncate">{timezone.replaceAll("_", " ")}</span>
          <ChevronDownIcon className="size-3.5 shrink-0" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="menu-surface w-[min(22rem,calc(100vw-2rem))] p-0">
        <Command label="Search timezones">
          <CommandInput placeholder="Search timezones…" aria-label="Search timezones" />
          <CommandList>
            <CommandEmpty>No timezone found.</CommandEmpty>
            {zones.map((zone) => (
              <CommandItem
                key={zone}
                value={zone}
                keywords={[zone.replaceAll("_", " ")]}
                onSelect={() => changeTimezone(zone)}
              >
                {zone.replaceAll("_", " ")}
                <CheckIcon className={cn("ml-auto size-4", zone !== timezone && "invisible")} />
              </CommandItem>
            ))}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
  const recurrenceFields = (
    <div className="flex min-w-0 flex-col gap-2" data-testid="schedule-fields">
      <div className="divide-y divide-border rounded-xl border border-border px-4">
        <div
          className="flex items-center justify-between gap-3 py-3"
          data-testid="schedule-frequency-time-row"
        >
          <span>Repeat</span>
          <ScheduleChoice
            label="Frequency"
            className={inlineChoiceClass}
            testId="schedule-preset-trigger"
            value={selectedFrequency}
            placeholder="Choose frequency"
            options={PRESETS.filter(
              (preset) =>
                anchoredScheduling ||
                (preset.value !== "custom" && preset.value !== "interval") ||
                (model.preset === "custom" && (preset.value === "custom" || minutely)),
            ).map((preset) => ({
              ...preset,
              disabled:
                (preset.value === "custom" || preset.value === "interval") && !anchoredScheduling,
            }))}
            onChange={changePreset}
            onOpenChange={onSelectOpenChange}
          />
        </div>
        {custom && (
          <>
            <div className="flex items-center justify-between gap-3 py-3">
              <span>Repeats</span>
              <ScheduleChoice
                label="Repeat unit"
                testId="custom-freq-trigger"
                className={inlineChoiceClass}
                disabled={!anchoredScheduling}
                value={model.customFreq}
                options={[
                  ...(minuteSchedulesSupported || minutely
                    ? [{ value: "minutely", label: "Minutely" }]
                    : []),
                  ...FREQUENCIES,
                ]}
                onChange={changeCustomFrequency}
                onOpenChange={onSelectOpenChange}
              />
            </div>
            <div className="flex items-center justify-between gap-3 py-3">
              <span>Every</span>
              <div className="flex items-center gap-2">
                {intervalInput}
                <span className="text-muted-foreground">
                  {INTERVAL_UNITS[model.customFreq]}
                  {model.interval === 1 ? "" : "s"}
                </span>
              </div>
            </div>
            {model.customFreq === "yearly" && (
              <div className="flex items-center justify-between gap-3 py-3">
                <span>Month</span>
                <ScheduleChoice
                  label="Month"
                  testId="schedule-month-trigger"
                  className={inlineChoiceClass}
                  disabled={!anchoredScheduling}
                  value={String(model.month)}
                  options={MONTHS}
                  onChange={(value) => onChange({ ...model, month: Number(value) })}
                  onOpenChange={onSelectOpenChange}
                />
              </div>
            )}
          </>
        )}
        {weekly && (
          <div className="flex min-w-0 items-center justify-between gap-3 py-3">
            <span>On</span>
            <ScheduleDaysPopover onOpenChange={onSelectOpenChange}>
              <PopoverTrigger asChild>
                <Button
                  variant="outline"
                  className={cn(
                    inlineChoiceClass,
                    "h-8 min-w-0 max-w-[85%] gap-2 px-0 font-normal",
                    pickerTriggerClass,
                  )}
                  disabled={custom && !anchoredScheduling}
                  aria-label="Days of the week"
                >
                  <span className="truncate">{weekdayLabel(model.weekdays)}</span>
                  <ChevronDownIcon className="size-3.5" />
                </Button>
              </PopoverTrigger>
              <PopoverContent align="end" className="w-auto max-w-[calc(100vw-2rem)] p-3">
                <WeekdayButtons
                  days={model.weekdays}
                  onChange={(weekdays) => onChange({ ...model, weekdays })}
                />
              </PopoverContent>
            </ScheduleDaysPopover>
          </div>
        )}
        {monthDays && (
          <div className="flex min-w-0 items-center justify-between gap-3 py-3">
            <span>On</span>
            <ScheduleDaysPopover onOpenChange={onSelectOpenChange}>
              <PopoverTrigger asChild>
                <Button
                  variant="outline"
                  disabled={!anchoredScheduling}
                  aria-label="Days of the month"
                  className={cn(
                    inlineChoiceClass,
                    "h-8 min-w-0 max-w-[85%] gap-2 px-0 font-normal",
                    pickerTriggerClass,
                  )}
                >
                  <span className="truncate">
                    {model.monthDays.length
                      ? [...model.monthDays].sort((a, b) => a - b).join(", ")
                      : "Select dates"}
                  </span>
                  <ChevronDownIcon className="size-3.5" />
                </Button>
              </PopoverTrigger>
              <PopoverContent align="end" className="w-[min(20rem,calc(100vw-2rem))] p-3">
                <div className="grid grid-cols-7 gap-1" role="group" aria-label="Days of the month">
                  {Array.from({ length: 31 }, (_, index) => index + 1).map((day) => (
                    <Button
                      key={day}
                      variant={model.monthDays.includes(day) ? "default" : "ghost"}
                      className="h-8 px-0"
                      aria-pressed={model.monthDays.includes(day)}
                      onClick={() =>
                        onChange({
                          ...model,
                          monthDays: model.monthDays.includes(day)
                            ? model.monthDays.filter((value) => value !== day)
                            : [...model.monthDays, day],
                        })
                      }
                    >
                      {day}
                    </Button>
                  ))}
                </div>
                <p className="mt-2 text-sm text-muted-foreground">
                  Months without a selected day are skipped.
                </p>
              </PopoverContent>
            </ScheduleDaysPopover>
          </div>
        )}
        {!(custom && minutely) && (
          <div className="flex min-w-0 items-center justify-between gap-3 py-3">
            <span className="shrink-0">{custom ? "Start time" : "Time"}</span>
            <div className="flex min-w-0 flex-1 items-center justify-end gap-2">
              {minutely && !custom && (
                <div className="flex items-center gap-2">
                  {intervalInput}
                  <span className="text-muted-foreground">minutes</span>
                </div>
              )}
              {!minutely &&
                (hourly ? (
                  <MinuteInput
                    showLabel
                    model={model}
                    onChange={onChange}
                    errorId={error ? errorId : undefined}
                  />
                ) : (
                  <ScheduleTimePicker
                    model={model}
                    onChange={onChange}
                    errorId={error ? errorId : undefined}
                  />
                ))}
              {!minutely && timezonePicker}
            </div>
          </div>
        )}
      </div>
      {error && (
        <p
          id={errorId}
          className="text-sm text-destructive"
          role="alert"
          data-testid="schedule-error"
        >
          {error}
        </p>
      )}
    </div>
  );
  const endingFields = (
    <div
      className="space-y-2 rounded-xl border border-border px-4 py-3"
      data-testid="schedule-ending-fields"
    >
      <div className="flex flex-wrap items-center gap-2 text-muted-foreground">
        <span className="mr-auto text-foreground">Ends</span>
        <ScheduleChoice
          className="border-0 bg-transparent text-muted-foreground shadow-none dark:bg-transparent"
          label="Schedule ends"
          value={model.end?.type ?? "never"}
          options={[
            { value: "never", label: "Never" },
            { value: "until", label: "On date" },
            ...(anchoredScheduling || model.end?.type === "count"
              ? [{ value: "count", label: "After", disabled: !anchoredScheduling }]
              : []),
          ]}
          onChange={changeEnd}
          onOpenChange={onSelectOpenChange}
        />
        {model.end?.type === "until" && (
          <>
            <Input
              type="date"
              aria-label="End date"
              aria-invalid={endingError ? true : undefined}
              aria-describedby={endingError ? endingErrorId : undefined}
              className="w-auto"
              value={activeDateInput?.value ?? (until ? dateInTimezone(until, timezone) : "")}
              onChange={(event) => changeEndDate(event.target.value, timezone)}
            />
            {minutely && timezonePicker}
          </>
        )}
        {model.end?.type === "count" && (
          <>
            <Input
              type="number"
              aria-label="Occurrences"
              aria-invalid={endingError ? true : undefined}
              aria-describedby={endingError ? endingErrorId : undefined}
              disabled={!anchoredScheduling}
              min={2}
              max={2147483647}
              step={1}
              className="w-24"
              value={Number.isFinite(model.end.count) ? model.end.count : ""}
              onChange={(event) =>
                onChange({
                  ...model,
                  end: {
                    type: "count",
                    count: event.target.value === "" ? NaN : Number(event.target.value),
                  },
                })
              }
            />
            <span>occurrences</span>
          </>
        )}
      </div>
      {model.end?.type === "count" && (
        <p className="text-sm text-muted-foreground">
          Counts scheduled occurrences since creation.
        </p>
      )}
      {endingError && (
        <p
          id={endingErrorId}
          className="text-sm text-destructive"
          role="alert"
          data-testid="schedule-ending-error"
        >
          {endingError}
        </p>
      )}
    </div>
  );
  return children ? (
    children({ recurrenceFields, endingFields })
  ) : (
    <div className="space-y-4">
      {recurrenceFields}
      {endingFields}
    </div>
  );
}

function ScheduleDaysPopover({
  children,
  onOpenChange,
}: {
  children: ReactNode;
  onOpenChange?: (open: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  function changeOpen(next: boolean) {
    setOpen(next);
    onOpenChange?.(next);
  }
  useScheduleEscape(open, () => {
    changeOpen(false);
    return true;
  });
  return (
    <Popover open={open} onOpenChange={changeOpen}>
      {children}
    </Popover>
  );
}

function ScheduleChoice({
  label,
  value,
  options,
  onChange,
  onOpenChange,
  testId,
  placeholder,
  className,
  disabled,
}: {
  label: string;
  value: string;
  options: { value: string; label: string; disabled?: boolean }[];
  onChange: (value: string) => void;
  onOpenChange?: (open: boolean) => void;
  testId?: string;
  placeholder?: string;
  className?: string;
  disabled?: boolean;
}) {
  return (
    <Select disabled={disabled} value={value} onValueChange={onChange} onOpenChange={onOpenChange}>
      <SelectTrigger
        aria-label={label}
        data-testid={testId}
        className={cn(choiceClass, className, pickerTriggerClass)}
      >
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent position="popper" align="end">
        {options.map((option) => (
          <SelectItem key={option.value} value={option.value} disabled={option.disabled}>
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function MinuteInput({
  showLabel = false,
  model,
  onChange,
  errorId,
}: {
  model: ScheduleModel;
  onChange: (next: ScheduleModel) => void;
  showLabel?: boolean;
  errorId?: string;
}) {
  const invalidMinute = !Number.isInteger(model.minute) || model.minute < 0 || model.minute > 59;
  return (
    <div className="flex shrink-0 items-center gap-2">
      {showLabel && <span className="text-muted-foreground">At minute</span>}
      <Input
        aria-label="Minute of the hour"
        aria-invalid={invalidMinute ? true : undefined}
        aria-describedby={invalidMinute ? errorId : undefined}
        data-testid="schedule-minute"
        type="number"
        min={0}
        max={59}
        step={1}
        value={Number.isFinite(model.minute) ? model.minute : ""}
        className="h-8 w-16 px-2 text-ui"
        onChange={(event) =>
          onChange({
            ...model,
            minute: event.target.value === "" ? NaN : Number(event.target.value),
          })
        }
      />
    </div>
  );
}

function WeekdayButtons({
  days,
  onChange,
}: {
  days: WeekdayCode[];
  onChange: (days: WeekdayCode[]) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1" role="group" aria-label="Weekdays">
      {WEEKDAY_CODES.map((day) => (
        <Button
          key={day}
          variant={days.includes(day) ? "default" : "ghost"}
          className="size-8 rounded-full p-0"
          aria-label={WEEKDAY_NAMES[day]}
          aria-pressed={days.includes(day)}
          data-testid={`weekday-${day}`}
          onClick={() =>
            onChange(days.includes(day) ? days.filter((value) => value !== day) : [...days, day])
          }
        >
          {WEEKDAY_NAMES[day].slice(0, 1)}
        </Button>
      ))}
    </div>
  );
}

function weekdayLabel(days: WeekdayCode[]): string {
  if (days.length === 0) return "Select days";
  if (days.length === 1) return WEEKDAY_NAMES[days[0]];
  return WEEKDAY_CODES.filter((day) => days.includes(day))
    .map((day) => WEEKDAY_NAMES[day].slice(0, 3))
    .join(", ");
}
