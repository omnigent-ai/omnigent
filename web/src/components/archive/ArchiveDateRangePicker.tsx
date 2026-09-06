import {
  CalendarIcon,
  ChevronDownIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  ChevronUpIcon,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { parseArchiveDateRange } from "@/lib/archiveDateRange";
import { cn } from "@/lib/utils";

export type ArchiveDatePreset = "any" | "lt7d" | "lt30d" | "lt365d";

const ARCHIVE_DATE_PRESETS: {
  value: Exclude<ArchiveDatePreset, "any">;
  label: string;
  ariaLabel: string;
  days: number;
}[] = [
  { value: "lt7d", label: "<7d", ariaLabel: "Last 7 days", days: 7 },
  { value: "lt30d", label: "<30d", ariaLabel: "Last 30 days", days: 30 },
  { value: "lt365d", label: "<1y", ariaLabel: "Last 1 year", days: 365 },
];

function formatDay(date: Date): string {
  return [date.getFullYear(), date.getMonth() + 1, date.getDate()]
    .map((part, index) => (index === 0 ? String(part) : String(part).padStart(2, "0")))
    .join("");
}

function sameDay(left: Date, right: Date): boolean {
  return formatDay(left) === formatDay(right);
}

type CalendarView = "days" | "months" | "years";
const ENGLISH_LOCALE = "en-US";

function monthLabel(year: number, month: number, format: "long" | "short" = "long"): string {
  return new Date(year, month, 1).toLocaleDateString(ENGLISH_LOCALE, { month: format });
}

function yearPageStart(year: number): number {
  const origin = 2020;
  return origin + Math.floor((year - origin) / 12) * 12;
}

function presetCalendarRange(preset: ArchiveDatePreset, ageReferenceSeconds?: number): string {
  const days = ARCHIVE_DATE_PRESETS.find((candidate) => candidate.value === preset)?.days;
  if (!days) return "";
  const end = new Date(ageReferenceSeconds === undefined ? Date.now() : ageReferenceSeconds * 1000);
  const start = new Date(end.getTime() - days * 86_400_000);
  return formatDay(start) + "-" + formatDay(end);
}

function monthDays(month: Date): { key: string; day: Date | null }[] {
  const first = new Date(month.getFullYear(), month.getMonth(), 1);
  const count = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate();
  const mondayOffset = (first.getDay() + 6) % 7;
  const monthKey = `${month.getFullYear()}-${month.getMonth() + 1}`;
  return [
    ...Array.from({ length: mondayOffset }, (_, index) => ({
      key: `${monthKey}-blank-${index + 1}`,
      day: null,
    })),
    ...Array.from({ length: count }, (_, index) => {
      const day = new Date(month.getFullYear(), month.getMonth(), index + 1);
      return { key: formatDay(day), day };
    }),
  ];
}

function CalendarGrid({
  value,
  onValueChange,
  focusRangeEnd = false,
}: {
  value: string;
  onValueChange: (value: string) => void;
  focusRangeEnd?: boolean;
}) {
  const parsed = parseArchiveDateRange(value);
  const focusedDate = focusRangeEnd ? parsed?.end : parsed?.start;
  const [month, setMonth] = useState(() => focusedDate ?? new Date());
  const [view, setView] = useState<CalendarView>("days");
  const [yearPage, setYearPage] = useState(() =>
    yearPageStart((focusedDate ?? new Date()).getFullYear()),
  );
  const [anchor, setAnchor] = useState<Date | null>(null);
  const pendingPickedValue = useRef<string | null>(null);
  const days = useMemo(() => monthDays(month), [month]);
  const parsedFocus = focusedDate?.getTime();

  useEffect(() => {
    if (parsedFocus === undefined) return;
    const next = new Date(parsedFocus);
    setMonth(new Date(next.getFullYear(), next.getMonth(), 1));
    if (pendingPickedValue.current === value) {
      pendingPickedValue.current = null;
    } else {
      setAnchor(null);
      setView("days");
      setYearPage(yearPageStart(next.getFullYear()));
    }
  }, [parsedFocus, value]);

  const pick = (day: Date) => {
    if (anchor === null) {
      setAnchor(day);
      const next = formatDay(day);
      pendingPickedValue.current = next;
      onValueChange(next);
      return;
    }
    const [start, end] = anchor <= day ? [anchor, day] : [day, anchor];
    const next = sameDay(start, end) ? formatDay(start) : `${formatDay(start)}-${formatDay(end)}`;
    pendingPickedValue.current = next;
    onValueChange(next);
    setAnchor(null);
  };

  return (
    <div data-testid="archive-calendar">
      {view === "days" && (
        <div className="mb-2 flex items-center justify-between">
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="Previous month"
            onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() - 1, 1))}
          >
            <ChevronLeftIcon />
          </Button>
          <div className="flex items-center">
            <button
              type="button"
              className="h-7 rounded-md px-2 text-sm font-medium hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
              aria-label="Choose year"
              onClick={() => {
                setYearPage(yearPageStart(month.getFullYear()));
                setView("years");
              }}
            >
              {String(month.getFullYear())}
            </button>
            <button
              type="button"
              className="h-7 rounded-md px-2 text-sm font-medium hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
              aria-label="Choose month"
              onClick={() => setView("months")}
            >
              {monthLabel(month.getFullYear(), month.getMonth())}
            </button>
          </div>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="Next month"
            onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() + 1, 1))}
          >
            <ChevronRightIcon />
          </Button>
        </div>
      )}
      {view === "months" && (
        <div className="mb-2 flex h-7 items-center justify-center text-sm font-medium">
          {String(month.getFullYear())}
        </div>
      )}
      {view === "years" && (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="mb-1 h-7 w-full"
          aria-label="Previous 12 years"
          onClick={() => setYearPage((current) => current - 12)}
        >
          <ChevronUpIcon />
        </Button>
      )}
      {view === "days" ? (
        <div className="grid grid-cols-7 gap-1 text-center text-xs text-muted-foreground">
          {[
            ["mon", "M"],
            ["tue", "T"],
            ["wed", "W"],
            ["thu", "T"],
            ["fri", "F"],
            ["sat", "S"],
            ["sun", "S"],
          ].map(([key, label]) => (
            <span key={key} className="py-1">
              {label}
            </span>
          ))}
          {days.map(({ key, day }) => {
            if (!day) return <span key={key} />;
            const selected = parsed !== null && day >= parsed.start && day <= parsed.end;
            return (
              <button
                key={key}
                type="button"
                aria-label={`${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`}
                aria-pressed={selected}
                className={cn(
                  "h-8 rounded-md text-sm text-foreground hover:bg-accent",
                  selected && "bg-primary/15 text-primary",
                  anchor && sameDay(day, anchor) && "ring-2 ring-primary/50",
                )}
                onClick={() => pick(day)}
              >
                {day.getDate()}
              </button>
            );
          })}
        </div>
      ) : view === "months" ? (
        <div className="grid grid-cols-3 gap-1" aria-label={`Months in ${month.getFullYear()}`}>
          {Array.from({ length: 12 }, (_, index) => (
            <button
              key={index}
              type="button"
              aria-label={`Choose ${month.getFullYear()}-${String(index + 1).padStart(2, "0")}`}
              aria-pressed={month.getMonth() === index}
              className={cn(
                "h-10 rounded-md text-sm text-foreground hover:bg-accent",
                month.getMonth() === index && "bg-primary/15 text-primary",
              )}
              onClick={() => {
                setMonth(new Date(month.getFullYear(), index, 1));
                setView("days");
              }}
            >
              {monthLabel(month.getFullYear(), index, "short")}
            </button>
          ))}
        </div>
      ) : (
        <>
          <div className="mb-1 text-center text-xs text-muted-foreground">
            {yearPage}–{yearPage + 11}
          </div>
          <div className="grid grid-cols-3 gap-1" aria-label="Years">
            {Array.from({ length: 12 }, (_, index) => yearPage + index).map((year) => (
              <button
                key={year}
                type="button"
                aria-label={`Choose year ${year}`}
                aria-pressed={month.getFullYear() === year}
                className={cn(
                  "h-10 rounded-md text-sm text-foreground hover:bg-accent",
                  month.getFullYear() === year && "bg-primary/15 text-primary",
                )}
                onClick={() => {
                  setMonth(new Date(year, month.getMonth(), 1));
                  setView("days");
                }}
              >
                {year}
              </button>
            ))}
          </div>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="mt-1 h-7 w-full"
            aria-label="Next 12 years"
            onClick={() => setYearPage((current) => current + 12)}
          >
            <ChevronDownIcon />
          </Button>
        </>
      )}
      <div className="mt-2 border-t pt-1">
        <Button
          type="button"
          variant="ghost"
          size="xs"
          className="h-7 w-full text-xs text-primary"
          aria-label="Today"
          onClick={() => {
            const today = new Date();
            setMonth(new Date(today.getFullYear(), today.getMonth(), 1));
            setView("days");
            setYearPage(yearPageStart(today.getFullYear()));
          }}
        >
          Today
        </Button>
      </div>
    </div>
  );
}

export function ArchiveDateRangePicker({
  value,
  onValueChange,
  className,
  inlineCalendar = false,
  agePreset = "any",
  ageReferenceSeconds,
  onAgePresetChange,
}: {
  value: string;
  onValueChange: (value: string) => void;
  className?: string;
  inlineCalendar?: boolean;
  agePreset?: ArchiveDatePreset;
  ageReferenceSeconds?: number;
  onAgePresetChange?: (preset: ArchiveDatePreset) => void;
}) {
  const [draft, setDraft] = useState(value);
  const [open, setOpen] = useState(false);
  const parsed = parseArchiveDateRange(draft);
  const invalid = draft.trim().length > 0 && parsed === null;
  const calendarValue = parsed ? draft : presetCalendarRange(agePreset, ageReferenceSeconds);

  useEffect(() => setDraft(value), [agePreset, value]);

  const changeManualValue = (next: string) => {
    setDraft(next);
    if (next === "") {
      onValueChange("");
      return;
    }
    if (!parseArchiveDateRange(next)) return;
    onValueChange(next);
    onAgePresetChange?.("any");
  };

  const input = (
    <Input
      value={draft}
      onChange={(event) => changeManualValue(event.target.value.replace(/[^0-9-]/g, ""))}
      aria-label="Archive day or date range"
      data-testid="archive-date-input"
      aria-invalid={invalid || undefined}
      placeholder="YYYYMMDD or YYYYMMDD-YYYYMMDD"
      maxLength={17}
      className={cn("h-9 min-w-0 font-mono text-xs", !inlineCalendar && "rounded-r-none")}
    />
  );

  if (inlineCalendar) {
    return (
      <div className={cn("min-w-0 space-y-3", className)}>
        <div>
          <div className="flex min-w-0 items-center gap-1">
            <div className="min-w-0 flex-1">{input}</div>
            {onAgePresetChange && (
              <div
                className="flex shrink-0 items-center gap-1"
                role="group"
                aria-label="Date presets"
              >
                {ARCHIVE_DATE_PRESETS.map((preset) => (
                  <Button
                    key={preset.value}
                    type="button"
                    variant="outline"
                    size="sm"
                    className={cn(
                      "h-9 w-10 px-1 text-xs font-normal",
                      agePreset === preset.value &&
                        "border-primary/40 bg-primary/15 text-primary dark:bg-primary/15",
                    )}
                    aria-label={preset.ariaLabel}
                    data-testid={`archive-date-preset-${preset.value}`}
                    aria-pressed={agePreset === preset.value}
                    onClick={() => {
                      setDraft("");
                      onValueChange("");
                      onAgePresetChange(preset.value);
                    }}
                  >
                    {preset.label}
                  </Button>
                ))}
              </div>
            )}
          </div>
          {invalid && (
            <p className="mt-1 text-[11px] text-destructive" role="alert">
              Use YYYYMMDD or YYYYMMDD-YYYYMMDD.
            </p>
          )}
        </div>
        {!invalid && (
          <CalendarGrid
            value={calendarValue}
            onValueChange={changeManualValue}
            focusRangeEnd={agePreset !== "any" && parsed === null}
          />
        )}
      </div>
    );
  }

  return (
    <div className={cn("min-w-0", className)}>
      <div className="flex min-w-0">
        <div className="min-w-0 flex-1">
          {input}
          {invalid && (
            <p className="mt-1 text-[11px] text-destructive" role="alert">
              Use YYYYMMDD or YYYYMMDD-YYYYMMDD.
            </p>
          )}
        </div>
        <Popover open={open} onOpenChange={(next) => setOpen(next && !invalid)}>
          <PopoverTrigger asChild>
            <Button
              type="button"
              variant="outline"
              className="h-9 shrink-0 rounded-l-none border-l-0 px-2.5"
              aria-label="Open archive calendar"
              disabled={invalid}
            >
              <CalendarIcon className="size-4" />
            </Button>
          </PopoverTrigger>
          <PopoverContent align="end" className="w-[19rem] p-3">
            <CalendarGrid value={calendarValue} onValueChange={changeManualValue} />
          </PopoverContent>
        </Popover>
      </div>
    </div>
  );
}
