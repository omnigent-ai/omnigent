import { useCallback, useEffect, useMemo, useState, type PointerEvent } from "react";
import { DicesIcon } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { isHexColor } from "@/lib/customTheme";
import { cn } from "@/lib/utils";

interface HsvColor {
  hue: number;
  saturation: number;
  value: number;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function hexToHsv(hex: string): HsvColor {
  const red = Number.parseInt(hex.slice(1, 3), 16) / 255;
  const green = Number.parseInt(hex.slice(3, 5), 16) / 255;
  const blue = Number.parseInt(hex.slice(5, 7), 16) / 255;
  const max = Math.max(red, green, blue);
  const min = Math.min(red, green, blue);
  const delta = max - min;

  let hue = 0;
  if (delta !== 0) {
    if (max === red) hue = 60 * (((green - blue) / delta) % 6);
    else if (max === green) hue = 60 * ((blue - red) / delta + 2);
    else hue = 60 * ((red - green) / delta + 4);
  }

  return {
    hue: hue < 0 ? hue + 360 : hue,
    saturation: max === 0 ? 0 : delta / max,
    value: max,
  };
}

function hsvToHex({ hue, saturation, value }: HsvColor): string {
  const chroma = value * saturation;
  const segment = hue / 60;
  const secondary = chroma * (1 - Math.abs((segment % 2) - 1));
  const offset = value - chroma;

  let red = 0;
  let green = 0;
  let blue = 0;
  if (segment < 1) [red, green, blue] = [chroma, secondary, 0];
  else if (segment < 2) [red, green, blue] = [secondary, chroma, 0];
  else if (segment < 3) [red, green, blue] = [0, chroma, secondary];
  else if (segment < 4) [red, green, blue] = [0, secondary, chroma];
  else if (segment < 5) [red, green, blue] = [secondary, 0, chroma];
  else [red, green, blue] = [chroma, 0, secondary];

  const channel = (component: number) =>
    Math.round((component + offset) * 255)
      .toString(16)
      .padStart(2, "0");
  return `#${channel(red)}${channel(green)}${channel(blue)}`;
}

function randomColor(): string {
  return hsvToHex({
    hue: Math.random() * 360,
    saturation: 0.55 + Math.random() * 0.35,
    value: 0.7 + Math.random() * 0.25,
  });
}

export function ThemeColorPicker({
  label,
  value,
  testId,
  onChange,
}: {
  label: string;
  value: string;
  testId: string;
  onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [draft, setDraft] = useState(value.toUpperCase());
  const color = useMemo(() => hexToHsv(value), [value]);

  useEffect(() => setDraft(value.toUpperCase()), [value]);

  const updateColor = useCallback((next: HsvColor) => onChange(hsvToHex(next)), [onChange]);

  const updateFromPointer = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      const bounds = event.currentTarget.getBoundingClientRect();
      updateColor({
        hue: color.hue,
        saturation: clamp((event.clientX - bounds.left) / bounds.width, 0, 1),
        value: 1 - clamp((event.clientY - bounds.top) / bounds.height, 0, 1),
      });
    },
    [color.hue, updateColor],
  );

  const updateDraft = useCallback(
    (next: string) => {
      setDraft(next.toUpperCase());
      const normalized = next.startsWith("#") ? next : `#${next}`;
      if (isHexColor(normalized)) onChange(normalized.toLowerCase());
    },
    [onChange],
  );

  return (
    <div className="flex items-center justify-between gap-4 border-b border-border/70 py-3 last:border-b-0">
      <span className="text-sm font-medium">{label}</span>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <button
            type="button"
            aria-label={`${label}: ${value.toUpperCase()}`}
            data-testid={`${testId}-trigger`}
            className={cn(
              "group flex h-10 w-44 items-center gap-2 rounded-xl border bg-background p-1.5 text-left shadow-xs outline-none transition-[border-color,box-shadow,transform]",
              "hover:border-border-strong hover:shadow-sm focus-visible:ring-2 focus-visible:ring-ring/40 active:scale-[0.99]",
              open && "border-primary/60 ring-2 ring-primary/15",
            )}
          >
            <span
              aria-hidden
              className="size-7 shrink-0 rounded-lg border border-white/25 shadow-sm ring-1 ring-black/10"
              style={{ backgroundColor: value }}
            />
            <span className="min-w-0 flex-1 font-mono text-xs font-medium tracking-wide">
              {value.toUpperCase()}
            </span>
          </button>
        </PopoverTrigger>
        <PopoverContent
          align="end"
          sideOffset={8}
          className="w-72 gap-0 overflow-hidden rounded-2xl border border-border/70 bg-popover p-0 shadow-2xl ring-1 ring-black/5"
        >
          <div className="flex items-center justify-between px-3.5 py-2.5">
            <span className="text-xs font-medium">{label}</span>
            <span className="font-mono text-[11px] text-muted-foreground">
              {value.toUpperCase()}
            </span>
          </div>
          <div className="px-2.5 pb-2.5">
            <div
              tabIndex={0}
              aria-label={`${label} saturation and brightness`}
              data-testid={`${testId}-spectrum`}
              onPointerDown={(event) => {
                event.currentTarget.setPointerCapture?.(event.pointerId);
                setDragging(true);
                updateFromPointer(event);
              }}
              onPointerMove={(event) => {
                if (dragging) updateFromPointer(event);
              }}
              onPointerUp={() => setDragging(false)}
              onPointerCancel={() => setDragging(false)}
              onKeyDown={(event) => {
                const step = event.shiftKey ? 0.05 : 0.01;
                const deltas: Record<string, [number, number]> = {
                  ArrowLeft: [-step, 0],
                  ArrowRight: [step, 0],
                  ArrowUp: [0, step],
                  ArrowDown: [0, -step],
                };
                const delta = deltas[event.key];
                if (!delta) return;
                event.preventDefault();
                updateColor({
                  hue: color.hue,
                  saturation: clamp(color.saturation + delta[0], 0, 1),
                  value: clamp(color.value + delta[1], 0, 1),
                });
              }}
              className="relative h-44 cursor-crosshair touch-none overflow-hidden rounded-xl outline-none ring-1 ring-black/10 focus-visible:ring-2 focus-visible:ring-ring"
              style={{
                backgroundColor: `hsl(${color.hue} 100% 50%)`,
                backgroundImage:
                  "linear-gradient(to top, #000, transparent), linear-gradient(to right, #fff, transparent)",
              }}
            >
              <span
                aria-hidden
                className="pointer-events-none absolute size-4 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white shadow-[0_1px_4px_rgb(0_0_0/0.75)] ring-1 ring-black/60"
                style={{
                  left: `${color.saturation * 100}%`,
                  top: `${(1 - color.value) * 100}%`,
                }}
              />
            </div>
          </div>
          <div className="border-t border-border/70 bg-muted/20 px-3 py-3">
            <input
              type="range"
              min="0"
              max="360"
              value={Math.round(color.hue)}
              aria-label={`${label} hue`}
              data-testid={`${testId}-hue`}
              onChange={(event) =>
                updateColor({ ...color, hue: Number.parseInt(event.target.value, 10) })
              }
              className="h-3 w-full cursor-pointer appearance-none rounded-full bg-[linear-gradient(to_right,#ff3b30,#ffcc00,#34c759,#00c7be,#0a84ff,#bf5af2,#ff2d55,#ff3b30)] shadow-inner outline-none [&::-moz-range-thumb]:size-5 [&::-moz-range-thumb]:rounded-full [&::-moz-range-thumb]:border-2 [&::-moz-range-thumb]:border-white [&::-moz-range-thumb]:bg-transparent [&::-moz-range-thumb]:shadow-md [&::-webkit-slider-thumb]:size-5 [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:border-2 [&::-webkit-slider-thumb]:border-white [&::-webkit-slider-thumb]:bg-transparent [&::-webkit-slider-thumb]:shadow-md"
            />
            <div className="mt-3 flex items-center gap-2">
              <span
                aria-hidden
                className="size-8 shrink-0 rounded-xl border border-white/25 shadow-sm ring-1 ring-black/10"
                style={{ backgroundColor: value }}
              />
              <input
                type="text"
                value={draft}
                aria-label={`${label} hex value`}
                data-testid={`${testId}-input`}
                maxLength={7}
                spellCheck={false}
                onChange={(event) => updateDraft(event.target.value)}
                onBlur={() => setDraft(value.toUpperCase())}
                className="h-9 min-w-0 flex-1 rounded-xl border bg-background px-3 font-mono text-xs font-medium uppercase tracking-wide outline-none transition-shadow focus:ring-2 focus:ring-ring/40"
              />
              <button
                type="button"
                aria-label={`Randomize ${label.toLowerCase()}`}
                title={`Randomize ${label.toLowerCase()}`}
                data-testid={`${testId}-randomize`}
                onClick={() => onChange(randomColor())}
                className="group/randomize flex size-9 shrink-0 items-center justify-center rounded-xl border bg-background text-muted-foreground outline-none transition-[background-color,border-color,color,transform] hover:border-primary/40 hover:bg-primary/10 hover:text-primary focus-visible:ring-2 focus-visible:ring-ring/40 active:scale-95"
              >
                <DicesIcon className="size-4 transition-transform duration-300 group-hover/randomize:-rotate-12 group-active/randomize:rotate-90" />
              </button>
            </div>
          </div>
        </PopoverContent>
      </Popover>
    </div>
  );
}
