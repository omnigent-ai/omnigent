// Editable time input shared by preset and custom automation schedules.
import { useEffect, useRef, useState, type RefObject } from "react";
import { CheckIcon } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Popover, PopoverAnchor, PopoverContent } from "@/components/ui/popover";
import { parseTimeOfDayInput, type ScheduleModel } from "@/lib/scheduleBuilder";
import { cn } from "@/lib/utils";
import { useScheduleEscape } from "./useScheduleEscape";

const HOURS = Array.from({ length: 24 }, (_, hour) => hour);
const MINUTES = Array.from({ length: 60 }, (_, minute) => minute);

export function ScheduleTimePicker({
  model,
  onChange,
  errorId,
  onOpenChange,
}: {
  model: ScheduleModel;
  onChange: (next: ScheduleModel) => void;
  errorId?: string;
  onOpenChange?: (open: boolean) => void;
}) {
  const invalidTime =
    !Number.isInteger(model.hour) ||
    model.hour < 0 ||
    model.hour > 23 ||
    !Number.isInteger(model.minute) ||
    model.minute < 0 ||
    model.minute > 59;
  const inputRef = useRef<HTMLInputElement | null>(null);
  const selectedHourRef = useRef<HTMLButtonElement | null>(null);
  const selectedMinuteRef = useRef<HTMLButtonElement | null>(null);
  const [timeText, setTimeText] = useState(() => formatTimeInput(model.hour, model.minute));
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (document.activeElement === inputRef.current) return;
    setTimeText(formatTimeInput(model.hour, model.minute));
  }, [model.hour, model.minute]);

  function changeOpen(next: boolean) {
    setOpen(next);
    onOpenChange?.(next);
  }

  useScheduleEscape(open, () => {
    changeOpen(false);
    return true;
  });

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

  function selectTime(nextHour: number, nextMinute: number) {
    const next = { ...model, hour: nextHour, minute: nextMinute };
    setTimeText(formatTimeInput(nextHour, nextMinute));
    onChange(next);
  }

  return (
    <Popover open={open} onOpenChange={changeOpen}>
      <PopoverAnchor asChild>
        <Input
          ref={inputRef}
          id="schedule-time"
          aria-label="Time"
          aria-expanded={open}
          aria-haspopup="dialog"
          value={timeText}
          data-testid="schedule-time"
          placeholder="5:00 PM"
          className="h-8 w-28 shrink-0 text-ui focus-visible:ring-0"
          aria-invalid={invalidTime ? true : undefined}
          aria-describedby={invalidTime ? errorId : undefined}
          onClick={() => changeOpen(true)}
          onChange={(event) => handleTimeTextChange(event.target.value)}
          onBlur={canonicalizeTimeText}
        />
      </PopoverAnchor>
      <PopoverContent
        align="end"
        className="menu-surface grid w-48 grid-cols-2 gap-0 overflow-hidden p-0 ring-0"
        data-testid="schedule-time-picker"
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          requestAnimationFrame(() => {
            centerSelectedOption(selectedHourRef.current);
            centerSelectedOption(selectedMinuteRef.current);
          });
        }}
      >
        <TimeColumn
          label="Hours"
          values={HOURS}
          selected={model.hour}
          selectedRef={selectedHourRef}
          onSelect={(hour) => selectTime(hour, model.minute)}
        />
        <TimeColumn
          label="Minutes"
          values={MINUTES}
          selected={model.minute}
          selectedRef={selectedMinuteRef}
          onSelect={(minute) => selectTime(model.hour, minute)}
          className="border-l border-border"
        />
      </PopoverContent>
    </Popover>
  );
}

function TimeColumn({
  label,
  values,
  selected,
  selectedRef,
  onSelect,
  className,
}: {
  label: string;
  values: number[];
  selected: number;
  selectedRef: RefObject<HTMLButtonElement | null>;
  onSelect: (value: number) => void;
  className?: string;
}) {
  return (
    <div
      role="listbox"
      aria-label={label}
      className={cn("max-h-56 overflow-y-auto overscroll-contain p-1.5", className)}
    >
      {values.map((value) => {
        const isSelected = value === selected;
        return (
          <button
            key={value}
            ref={isSelected ? selectedRef : undefined}
            type="button"
            role="option"
            aria-selected={isSelected}
            className={cn(
              "flex h-9 w-full items-center justify-center rounded-lg text-ui outline-none hover:bg-muted focus-visible:bg-muted",
              isSelected && "bg-muted font-medium",
            )}
            onClick={() => onSelect(value)}
          >
            {pad(value)}
            <CheckIcon className="sr-only" />
          </button>
        );
      })}
    </div>
  );
}

function centerSelectedOption(option: HTMLButtonElement | null) {
  const list = option?.parentElement;
  if (!option || !list) return;
  list.scrollTop = option.offsetTop - (list.clientHeight - option.clientHeight) / 2;
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
  period: "AM" | "PM";
} {
  const hour = Math.min(23, Math.max(0, time.hour));
  return {
    hour12: hour % 12 === 0 ? 12 : hour % 12,
    minute: Math.min(59, Math.max(0, time.minute)),
    period: hour >= 12 ? "PM" : "AM",
  };
}
