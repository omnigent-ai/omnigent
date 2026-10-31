// Shared typed time input and picker for preset and custom automation schedules.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { ClockIcon } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Popover, PopoverAnchor, PopoverContent } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import { useScheduleEscape } from "./useScheduleEscape";
import {
  DEFAULT_SCHEDULE_MODEL,
  parseTimeOfDayInput,
  type ScheduleModel,
} from "@/lib/scheduleBuilder";

const HOURS_12 = Array.from({ length: 12 }, (_, i) => i + 1);
const MINUTES = Array.from({ length: 60 }, (_, i) => i);
const PERIODS = ["AM", "PM"] as const;
type Period = (typeof PERIODS)[number];

export function ScheduleTimePicker({
  model,
  onChange,
  onSelectOpenChange,
}: {
  model: ScheduleModel;
  onChange: (next: ScheduleModel) => void;
  onSelectOpenChange?: (open: boolean) => void;
}) {
  const invalidTime =
    !Number.isInteger(model.hour) ||
    model.hour < 0 ||
    model.hour > 23 ||
    !Number.isInteger(model.minute) ||
    model.minute < 0 ||
    model.minute > 59;
  const inputRef = useRef<HTMLInputElement | null>(null);
  const hourColumnRef = useRef<HTMLDivElement | null>(null);
  const minuteColumnRef = useRef<HTMLDivElement | null>(null);
  const periodColumnRef = useRef<HTMLDivElement | null>(null);
  const [timeText, setTimeText] = useState(() => formatTimeInput(model.hour, model.minute));
  const [timePickerOpen, setTimePickerOpen] = useState(false);
  useScheduleEscape(timePickerOpen, () => {
    handleTimePickerOpenChange(false);
    return true;
  });

  useEffect(() => {
    if (document.activeElement === inputRef.current) return;
    setTimeText(formatTimeInput(model.hour, model.minute));
  }, [model.hour, model.minute]);

  const pickerParts = toPickerParts(getPickerTime());

  useEffect(() => {
    if (!timePickerOpen) return;
    scrollSelectedIntoView(hourColumnRef.current);
    scrollSelectedIntoView(minuteColumnRef.current);
  }, [pickerParts.hour12, pickerParts.minute, timePickerOpen]);

  useEffect(() => {
    if (!timePickerOpen) return;
    const handleWheel = (event: WheelEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      const columns = [hourColumnRef.current, minuteColumnRef.current, periodColumnRef.current];
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
  }, [timePickerOpen]);

  function handleTimeTextChange(value: string) {
    setTimeText(value);
    const parsed = parseTimeOfDayInput(value);
    onChange({
      ...model,
      hour: parsed?.hour ?? Number.NaN,
      minute: parsed?.minute ?? Number.NaN,
    });
  }

  function canonicalizeTimeText() {
    const parsed = parseTimeOfDayInput(timeText);
    if (parsed !== null) setTimeText(formatTimeInput(parsed.hour, parsed.minute));
  }

  function handleTimePickerOpenChange(next: boolean) {
    if (next === timePickerOpen) return;
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

  return (
    <Popover open={timePickerOpen} onOpenChange={handleTimePickerOpenChange}>
      <PopoverAnchor asChild>
        <div className="relative">
          <Input
            ref={inputRef}
            id="schedule-time"
            aria-label="Time"
            value={timeText}
            data-testid="schedule-time"
            placeholder="5:00 PM"
            className="h-8 w-32 pr-8 text-ui"
            aria-invalid={invalidTime ? true : undefined}
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
        onInteractOutside={(event) => {
          if (
            event.target instanceof Node &&
            inputRef.current?.parentElement?.contains(event.target)
          ) {
            event.preventDefault();
          }
        }}
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
  );
}

function pad(n: number): string {
  return n.toString().padStart(2, "0");
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
