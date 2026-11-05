// Editable time input shared by preset and custom automation schedules.
import { useEffect, useRef, useState } from "react";
import { Input } from "@/components/ui/input";
import { parseTimeOfDayInput, type ScheduleModel } from "@/lib/scheduleBuilder";

export function ScheduleTimePicker({
  model,
  onChange,
  errorId,
}: {
  model: ScheduleModel;
  onChange: (next: ScheduleModel) => void;
  errorId?: string;
}) {
  const invalidTime =
    !Number.isInteger(model.hour) ||
    model.hour < 0 ||
    model.hour > 23 ||
    !Number.isInteger(model.minute) ||
    model.minute < 0 ||
    model.minute > 59;
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [timeText, setTimeText] = useState(() => formatTimeInput(model.hour, model.minute));

  useEffect(() => {
    if (document.activeElement === inputRef.current) return;
    setTimeText(formatTimeInput(model.hour, model.minute));
  }, [model.hour, model.minute]);

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

  return (
    <Input
      ref={inputRef}
      id="schedule-time"
      aria-label="Time"
      value={timeText}
      data-testid="schedule-time"
      placeholder="5:00 PM"
      className="h-8 w-28 shrink-0 text-ui focus-visible:ring-0"
      aria-invalid={invalidTime ? true : undefined}
      aria-describedby={invalidTime ? errorId : undefined}
      onChange={(event) => handleTimeTextChange(event.target.value)}
      onBlur={canonicalizeTimeText}
    />
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
  period: "AM" | "PM";
} {
  const hour = Math.min(23, Math.max(0, time.hour));
  return {
    hour12: hour % 12 === 0 ? 12 : hour % 12,
    minute: Math.min(59, Math.max(0, time.minute)),
    period: hour >= 12 ? "PM" : "AM",
  };
}
