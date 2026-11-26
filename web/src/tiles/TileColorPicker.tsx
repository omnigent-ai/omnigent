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
  /** The active tile's edge is wider. */
  active?: boolean;
  /** Quiet agents show a grey edge. */
  muted?: boolean;
}

export function TileColorPicker({
  sessionId,
  title,
  color,
  active = false,
  muted = false,
}: TileColorPickerProps) {
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
          className={cn(
            "shrink-0 cursor-pointer self-stretch transition-[width] hover:w-[9px]",
            active ? "w-[7px]" : "w-[5px]",
            muted && "bg-muted-foreground/50",
          )}
          style={muted ? undefined : { backgroundColor: color.solid }}
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
