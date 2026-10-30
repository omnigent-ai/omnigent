// Sentence-style schedule controls; custom edits are applied as one recurrence.
import { useMemo, useRef, useState } from "react";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Label } from "@/components/scheduled/Label";
import { ScheduleTimePicker } from "./ScheduleTimePicker";
import { useScheduleEscape } from "./useScheduleEscape";
import { shouldGuardDialogDismiss } from "@/lib/dialogDismissGuard";
import { cn } from "@/lib/utils";
import { commonTimezones, dateInTimezone, endOfDayInTimezone } from "@/lib/timezones";
import { describeSchedule } from "@/lib/scheduleText";
import {
  buildRRule,
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
  { value: "hourly", label: "Every hour" },
  { value: "daily", label: "Every day" },
  { value: "weekdays", label: "Every weekday" },
  { value: "weekly", label: "Every week" },
  { value: "custom", label: "Custom schedule" },
];
const FREQUENCIES = [
  { value: "hourly", label: "Hours" },
  { value: "daily", label: "Days" },
  { value: "weekly", label: "Weeks" },
  { value: "monthly", label: "Months" },
  { value: "yearly", label: "Years" },
];
const MINUTES = Array.from({ length: 60 }, (_, minute) => ({
  value: String(minute),
  label: String(minute).padStart(2, "0"),
}));
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

export function ScheduleFields({
  model,
  anchoredScheduling,
  onChange,
  timezone,
  onTimezoneChange,
  onSelectOpenChange,
  onRecurrenceSelect,
  recurrenceUnsupported = false,
  allowPastEnd = false,
}: {
  model: ScheduleModel;
  anchoredScheduling: boolean;
  onChange: (next: ScheduleModel) => void;
  timezone: string;
  onTimezoneChange: (timezone: string) => void;
  onSelectOpenChange?: (open: boolean) => void;
  onRecurrenceSelect?: () => void;
  recurrenceUnsupported?: boolean;
  allowPastEnd?: boolean;
}) {
  const [customOpen, setCustomOpen] = useState(false);
  const [draft, setDraft] = useState(model);
  const [timezoneOpen, setTimezoneOpen] = useState(false);
  const nestedOpenCount = useRef(0);
  const nestedClosedAt = useRef(0);
  function handleNestedOpenChange(open: boolean) {
    nestedOpenCount.current = Math.max(0, nestedOpenCount.current + (open ? 1 : -1));
    if (!open) nestedClosedAt.current = Date.now();
    onSelectOpenChange?.(open);
  }
  useScheduleEscape(customOpen, () => {
    if (nestedOpenCount.current > 0) return false;
    changeCustomOpen(false);
    return true;
  });
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
  const error = activeDateInput?.error ?? validateSchedule(model, { allowPastEnd });
  const recurrence = { ...model, end: undefined };
  const recurrenceError = validateSchedule(recurrence);
  const draftError = validateSchedule({ ...draft, end: undefined });
  const until = model.end?.type === "until" ? parseUntil(model.end.until) : null;

  function changeCustomOpen(open: boolean) {
    if (open) setDraft(model);
    setCustomOpen(open);
    onSelectOpenChange?.(open);
  }

  function changePreset(value: string) {
    const next = { ...model, preset: value as SchedulePreset };
    if (value === "hourly" && !Number.isInteger(next.minute)) next.minute = 0;
    if (value === "custom") {
      if (model.preset === "weekdays") next.customFreq = "weekly";
      else if (model.preset !== "custom") next.customFreq = model.preset;
      if (model.preset === "weekdays") next.weekdays = ["MO", "TU", "WE", "TH", "FR"];
      if (model.preset !== "custom") next.interval = 1;
      setDraft(next);
      setCustomOpen(true);
      onSelectOpenChange?.(true);
      return;
    }
    onChange(next);
    onRecurrenceSelect?.();
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
    if (model.end?.type === "until") {
      changeEndDate(activeDateInput?.value ?? (until ? dateInTimezone(until, timezone) : ""), next);
    }
    onTimezoneChange(next);
    setTimezoneOpen(false);
    onSelectOpenChange?.(false);
  }

  let selectedFrequency = recurrenceUnsupported ? "" : model.preset;
  if (customOpen) selectedFrequency = "custom";

  return (
    <div className="flex min-w-0 flex-col gap-1.5" data-testid="schedule-fields">
      <Label>Schedule</Label>
      <div className="flex flex-wrap items-center gap-2" data-testid="schedule-frequency-time-row">
        <ScheduleChoice
          label="Frequency"
          className="min-w-36"
          testId="schedule-preset-trigger"
          preventCloseAutoFocus={customOpen}
          value={selectedFrequency}
          placeholder="Choose frequency"
          options={PRESETS.filter(
            (preset) =>
              preset.value !== "custom" || anchoredScheduling || model.preset === "custom",
          ).map((preset) => ({
            ...preset,
            disabled: preset.value === "custom" && !anchoredScheduling,
          }))}
          onChange={changePreset}
          onOpenChange={onSelectOpenChange}
        />
        {model.preset === "custom" || customOpen ? (
          <Popover open={customOpen} onOpenChange={changeCustomOpen}>
            <PopoverTrigger asChild>
              <Button
                variant="outline"
                className="h-8 min-w-0 max-w-72 justify-between px-2.5 font-normal"
                data-testid="custom-schedule-trigger"
                disabled={!anchoredScheduling}
              >
                <span className="truncate">
                  {model.preset !== "custom" || recurrenceError
                    ? "Select schedule…"
                    : describeSchedule(buildRRule(recurrence))}
                </span>
                <ChevronDownIcon className="size-3.5 shrink-0" />
              </Button>
            </PopoverTrigger>
            <PopoverContent
              align="start"
              className="max-h-(--radix-popover-content-available-height) w-[min(24rem,calc(100vw-2rem))] gap-4 overflow-y-auto p-4"
              onInteractOutside={(event) => {
                if (
                  shouldGuardDialogDismiss(event.target, {
                    selectOpen: nestedOpenCount.current > 0,
                    msSinceSelectClose: Date.now() - nestedClosedAt.current,
                  })
                )
                  event.preventDefault();
              }}
            >
              <div className="flex flex-col gap-2">
                <Label htmlFor="custom-interval">Repeat every</Label>
                <div className="flex items-center gap-2">
                  <Input
                    id="custom-interval"
                    data-testid="custom-interval"
                    type="number"
                    min={1}
                    max={366}
                    step={1}
                    value={Number.isFinite(draft.interval) ? draft.interval : ""}
                    className="w-24"
                    onChange={(event) =>
                      setDraft({
                        ...draft,
                        interval: event.target.value === "" ? NaN : Number(event.target.value),
                      })
                    }
                  />
                  <ScheduleChoice
                    label="Repeat unit"
                    testId="custom-freq-trigger"
                    value={draft.customFreq}
                    options={FREQUENCIES}
                    onChange={(value) => {
                      const next = { ...draft, customFreq: value as CustomFreq };
                      if (value === "hourly" && !Number.isInteger(next.minute)) next.minute = 0;
                      setDraft(next);
                    }}
                    onOpenChange={handleNestedOpenChange}
                  />
                </div>
              </div>
              {draft.customFreq === "weekly" && (
                <div className="flex flex-col gap-2">
                  <Label>Repeat on</Label>
                  <WeekdayButtons
                    days={draft.weekdays}
                    onChange={(weekdays) => setDraft({ ...draft, weekdays })}
                  />
                </div>
              )}
              {draft.customFreq === "yearly" && (
                <div className="flex flex-col gap-2">
                  <Label>Month</Label>
                  <ScheduleChoice
                    label="Month"
                    testId="schedule-month-trigger"
                    value={String(draft.month)}
                    options={MONTHS}
                    onChange={(value) => setDraft({ ...draft, month: Number(value) })}
                    onOpenChange={handleNestedOpenChange}
                  />
                </div>
              )}
              {(draft.customFreq === "monthly" || draft.customFreq === "yearly") && (
                <div className="flex flex-col gap-2">
                  <Label>Days of the month</Label>
                  <div
                    className="grid grid-cols-7 gap-1"
                    role="group"
                    aria-label="Days of the month"
                  >
                    {Array.from({ length: 31 }, (_, index) => index + 1).map((day) => (
                      <Button
                        key={day}
                        variant={draft.monthDays.includes(day) ? "default" : "ghost"}
                        className="h-8 px-0"
                        aria-pressed={draft.monthDays.includes(day)}
                        onClick={() =>
                          setDraft({
                            ...draft,
                            monthDays: draft.monthDays.includes(day)
                              ? draft.monthDays.filter((value) => value !== day)
                              : [...draft.monthDays, day],
                          })
                        }
                      >
                        {day}
                      </Button>
                    ))}
                  </div>
                  <p className="text-sm text-muted-foreground">
                    Months without a selected day are skipped.
                  </p>
                </div>
              )}
              <div className="flex flex-col gap-2">
                <Label>
                  {draft.customFreq === "hourly"
                    ? "At minute"
                    : `Time (${timezone.replaceAll("_", " ")})`}
                </Label>
                {draft.customFreq === "hourly" ? (
                  <MinuteChoice
                    model={draft}
                    onChange={setDraft}
                    onOpenChange={handleNestedOpenChange}
                  />
                ) : (
                  <ScheduleTimePicker
                    model={draft}
                    onChange={setDraft}
                    onSelectOpenChange={handleNestedOpenChange}
                  />
                )}
              </div>
              {draftError && (
                <p className="text-sm text-destructive" role="alert">
                  {draftError}
                </p>
              )}
              <Button
                disabled={!anchoredScheduling || draftError !== null}
                onClick={() => {
                  onChange(draft);
                  onRecurrenceSelect?.();
                  changeCustomOpen(false);
                }}
              >
                Apply
              </Button>
            </PopoverContent>
          </Popover>
        ) : (
          <>
            {model.preset === "weekly" && (
              <div className="flex max-w-full items-center gap-2">
                <span className="text-muted-foreground">on</span>
                <Popover onOpenChange={onSelectOpenChange}>
                  <PopoverTrigger asChild>
                    <Button
                      variant="outline"
                      className="h-8 px-2.5 font-normal"
                      aria-label="Days of the week"
                    >
                      {weekdayLabel(model.weekdays)}
                      <ChevronDownIcon className="size-3.5" />
                    </Button>
                  </PopoverTrigger>
                  <PopoverContent align="start" className="w-auto max-w-[calc(100vw-2rem)] p-3">
                    <WeekdayButtons
                      days={model.weekdays}
                      onChange={(weekdays) => onChange({ ...model, weekdays })}
                    />
                  </PopoverContent>
                </Popover>
              </div>
            )}
            <div className="flex items-center gap-2">
              <span className="text-muted-foreground">
                {model.preset === "hourly" ? "at minute" : "at"}
              </span>
              {model.preset === "hourly" ? (
                <MinuteChoice model={model} onChange={onChange} onOpenChange={onSelectOpenChange} />
              ) : (
                <ScheduleTimePicker
                  model={model}
                  onChange={onChange}
                  onSelectOpenChange={onSelectOpenChange}
                />
              )}
            </div>
          </>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-0 max-w-full">
          <Popover
            open={timezoneOpen}
            onOpenChange={(open) => {
              setTimezoneOpen(open);
              onSelectOpenChange?.(open);
            }}
          >
            <PopoverTrigger asChild>
              <Button
                variant="outline"
                className="h-8 max-w-full min-w-0 justify-between gap-2 px-2.5 font-normal"
                role="combobox"
                aria-label="Timezone"
                aria-expanded={timezoneOpen}
                data-testid="task-timezone-trigger"
              >
                <span className="truncate">{timezone.replaceAll("_", " ")}</span>
                <ChevronDownIcon className="size-3.5 shrink-0" />
              </Button>
            </PopoverTrigger>
            <PopoverContent align="start" className="w-[min(22rem,calc(100vw-2rem))] p-0">
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
                      <CheckIcon
                        className={cn("ml-auto size-4", zone !== timezone && "invisible")}
                      />
                    </CommandItem>
                  ))}
                </CommandList>
              </Command>
            </PopoverContent>
          </Popover>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-muted-foreground">
          <span>ends</span>
          <ScheduleChoice
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
            <Input
              type="date"
              aria-label="End date"
              className="w-auto"
              value={activeDateInput?.value ?? (until ? dateInTimezone(until, timezone) : "")}
              onChange={(event) => changeEndDate(event.target.value, timezone)}
            />
          )}
          {model.end?.type === "count" && (
            <>
              <Input
                type="number"
                aria-label="Occurrences"
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
      </div>
      {model.end?.type === "count" && (
        <p className="text-sm text-muted-foreground">
          Counts scheduled occurrences since creation.
        </p>
      )}
      {model.preset === "hourly" && Number.isInteger(model.minute) && (
        <p className="text-sm text-muted-foreground">{hourlyHint(model.minute)}</p>
      )}
      {error && (
        <p className="text-sm text-destructive" role="alert" data-testid="schedule-error">
          {error}
        </p>
      )}
    </div>
  );
}

function ScheduleChoice({
  label,
  value,
  options,
  onChange,
  onOpenChange,
  testId,
  preventCloseAutoFocus,
  placeholder,
  className,
}: {
  label: string;
  value: string;
  options: { value: string; label: string; disabled?: boolean }[];
  onChange: (value: string) => void;
  onOpenChange?: (open: boolean) => void;
  testId?: string;
  preventCloseAutoFocus?: boolean;
  placeholder?: string;
  className?: string;
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
    <Select open={open} value={value} onValueChange={onChange} onOpenChange={changeOpen}>
      <SelectTrigger aria-label={label} data-testid={testId} className={cn(choiceClass, className)}>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent
        position="popper"
        align="start"
        onCloseAutoFocus={(event) => {
          if (preventCloseAutoFocus) event.preventDefault();
        }}
      >
        {options.map((option) => (
          <SelectItem key={option.value} value={option.value} disabled={option.disabled}>
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function MinuteChoice({
  model,
  onChange,
  onOpenChange,
}: {
  model: ScheduleModel;
  onChange: (next: ScheduleModel) => void;
  onOpenChange?: (open: boolean) => void;
}) {
  return (
    <ScheduleChoice
      label="Minute of the hour"
      testId="schedule-minute"
      value={String(model.minute)}
      options={MINUTES}
      onChange={(value) => onChange({ ...model, minute: Number(value) })}
      onOpenChange={onOpenChange}
    />
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

function hourlyHint(minute: number): string {
  if (minute === 0) return "Runs at the start of every hour.";
  const unit = minute === 1 ? "minute" : "minutes";
  return `Runs at ${minute} ${unit} past every hour.`;
}

function weekdayLabel(days: WeekdayCode[]): string {
  if (days.length === 0) return "Select days";
  if (days.length === 1) return WEEKDAY_NAMES[days[0]];
  return WEEKDAY_CODES.filter((day) => days.includes(day))
    .map((day) => WEEKDAY_NAMES[day].slice(0, 3))
    .join(", ");
}
