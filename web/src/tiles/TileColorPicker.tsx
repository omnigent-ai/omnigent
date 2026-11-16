// The tile's color stripe; clicking it opens twelve swatches and Auto.

import { useState } from "react";
import { CheckIcon } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import { setTileHue, TILE_HUES, type TileColor } from "./tileColors";

interface TileColorPickerProps {
  sessionId: string;
  title: string;
  color: TileColor;
}

export function TileColorPicker({ sessionId, title, color }: TileColorPickerProps) {
  const [open, setOpen] = useState(false);
  const pick = (hue: number | null) => {
    setTileHue(sessionId, hue);
    setOpen(false);
  };
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={`Tile color for ${title}`}
          title="Tile color"
          data-testid="tile-color-stripe"
          className="h-5 w-1.5 shrink-0 cursor-pointer rounded-full transition-[width] hover:w-2.5"
          style={{ backgroundColor: color.solid }}
        />
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="w-auto p-2"
        onOpenAutoFocus={(event) => event.preventDefault()}
      >
        <div role="group" aria-label="Tile color" className="grid grid-cols-6 gap-1.5">
          {TILE_HUES.map(({ name, hue }) => {
            const selected = color.picked && color.hue === hue;
            return (
              <button
                key={name}
                type="button"
                aria-label={name}
                aria-pressed={selected}
                title={name}
                onClick={() => pick(hue)}
                className="flex size-6 items-center justify-center rounded-full ring-offset-2 ring-offset-background focus-visible:ring-2 focus-visible:ring-ring"
                style={{ backgroundColor: `hsl(${hue} 70% 52%)` }}
              >
                {selected && <CheckIcon className="size-3.5 text-white" aria-hidden />}
              </button>
            );
          })}
        </div>
        <button
          type="button"
          onClick={() => pick(null)}
          aria-pressed={!color.picked}
          className={cn(
            "mt-2 w-full rounded-md px-2 py-1 text-left text-xs hover:bg-accent",
            !color.picked && "font-medium",
          )}
        >
          Auto{!color.picked && " ✓"}
        </button>
      </PopoverContent>
    </Popover>
  );
}
