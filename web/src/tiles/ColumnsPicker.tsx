// "Columns" control for the Gallery and Cards: hover across the boxes (like
// picking a table size) and click, or choose Auto to fit the window.

import { useState } from "react";
import { Columns3Icon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import { MAX_COLUMNS } from "./tileLayout";

interface ColumnsPickerProps {
  /** Chosen count; 0 = Auto. */
  columns: number;
  /** Columns this window fits, to explain when the choice is capped. */
  fitting: number;
  onChange: (columns: number) => void;
}

export function ColumnsPicker({ columns, fitting, onChange }: ColumnsPickerProps) {
  const [open, setOpen] = useState(false);
  const [hovered, setHovered] = useState(0);
  const shown = hovered || columns;
  const capped = columns > 0 && fitting < columns;
  const choose = (value: number) => {
    onChange(value);
    setOpen(false);
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="xs"
          className="gap-1.5"
          aria-label="Columns"
          title={
            capped
              ? `Up to ${columns} columns. This window fits ${fitting}, so tiles wrap.`
              : "How many columns the tiles use on this display"
          }
        >
          <Columns3Icon className="size-3.5" />
          {columns === 0 ? "Auto" : capped ? `${fitting} of ${columns}` : columns}
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        className="w-auto p-2"
        // Auto-focusing the first box would read as choosing one column.
        onOpenAutoFocus={(event) => event.preventDefault()}
      >
        <div
          className="flex gap-1"
          onMouseLeave={() => setHovered(0)}
          role="group"
          aria-label="Column count"
        >
          {Array.from({ length: MAX_COLUMNS }, (_, index) => index + 1).map((count) => (
            <button
              key={count}
              type="button"
              aria-label={`${count} column${count === 1 ? "" : "s"}`}
              aria-pressed={columns === count}
              onMouseEnter={() => setHovered(count)}
              onFocus={() => setHovered(count)}
              onClick={() => choose(count)}
              className={cn(
                "h-8 w-5 rounded-sm border transition-colors",
                count <= shown ? "border-brand-accent bg-brand-accent/30" : "bg-muted/40",
              )}
            />
          ))}
        </div>
        <div className="mt-2 flex items-center justify-between gap-3 text-xs text-muted-foreground">
          <span>{shown ? `${shown} column${shown === 1 ? "" : "s"}` : "Fit to window"}</span>
          <Button
            variant={columns === 0 ? "secondary" : "ghost"}
            size="xs"
            onClick={() => choose(0)}
          >
            Auto
          </Button>
        </div>
        <p className="mt-1.5 text-[11px] text-muted-foreground">Remembered for this display.</p>
      </PopoverContent>
    </Popover>
  );
}
