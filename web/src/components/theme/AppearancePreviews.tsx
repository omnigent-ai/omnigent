import { ArchiveIcon, Trash2Icon } from "lucide-react";
import type { SwipeAction, SwipeDirection } from "@/lib/swipeActionPreferences";
import type { PaletteSwatch } from "@/lib/themePalette";
import { cn } from "@/lib/utils";
import type { ThemeMode } from "./themeMode";

export const LIGHT_MODE_PREVIEW: PaletteSwatch = {
  bg: "#e9ebee",
  card: "#ffffff",
  accent: "#aab2bd",
  border: "#d7dbe0",
  text: "#11171c",
};

export const DARK_MODE_PREVIEW: PaletteSwatch = {
  bg: "#0e1013",
  card: "#232a33",
  accent: "#5b6672",
  border: "#2b333d",
  text: "#e6edf3",
};

export function ModePreview({ variant }: { variant: ThemeMode }) {
  if (variant === "light") return <PaletteSwatchPreview swatch={LIGHT_MODE_PREVIEW} />;
  if (variant === "dark") return <PaletteSwatchPreview swatch={DARK_MODE_PREVIEW} />;
  return (
    <div className="relative h-16 w-full">
      <PaletteSwatchPreview swatch={LIGHT_MODE_PREVIEW} />
      <div
        aria-hidden
        className="absolute inset-0"
        style={{ clipPath: "polygon(62% 0, 100% 0, 100% 100%, 38% 100%)" }}
      >
        <PaletteSwatchPreview swatch={DARK_MODE_PREVIEW} />
      </div>
    </div>
  );
}

export function PaletteChip({ swatch }: { swatch: PaletteSwatch }) {
  return (
    <span
      aria-hidden
      className="flex size-5 shrink-0 items-center justify-center rounded-md border"
      style={{ backgroundColor: swatch.bg, borderColor: swatch.border }}
    >
      <span className="size-2 rounded-full" style={{ backgroundColor: swatch.accent }} />
    </span>
  );
}

export function PaletteSwatchPreview({ swatch }: { swatch: PaletteSwatch }) {
  return (
    <div
      aria-hidden
      className="flex h-16 w-full gap-1.5 overflow-hidden rounded-lg p-1.5"
      style={{ backgroundColor: swatch.bg, border: `1px solid ${swatch.border}` }}
    >
      <div
        className="flex w-1/3 flex-col gap-1 rounded-md p-1"
        style={{ backgroundColor: swatch.card, border: `1px solid ${swatch.border}` }}
      >
        <div className="size-1.5 rounded-full" style={{ backgroundColor: swatch.accent }} />
        <div
          className="h-1 w-4/5 rounded-full"
          style={{ backgroundColor: swatch.text, opacity: 0.35 }}
        />
        <div
          className="h-1 w-3/5 rounded-full"
          style={{ backgroundColor: swatch.text, opacity: 0.25 }}
        />
      </div>
      <div
        className="flex flex-1 flex-col gap-1 rounded-md p-1.5"
        style={{ backgroundColor: swatch.card, border: `1px solid ${swatch.border}` }}
      >
        <div
          className="h-1 w-3/4 rounded-full"
          style={{ backgroundColor: swatch.text, opacity: 0.5 }}
        />
        <div
          className="h-1 w-1/2 rounded-full"
          style={{ backgroundColor: swatch.text, opacity: 0.3 }}
        />
        <div className="mt-auto h-2.5 w-2/5 rounded" style={{ backgroundColor: swatch.accent }} />
      </div>
    </div>
  );
}

/** A session row's status dot and title bar. */
function PreviewRowMarks({ swatch, opacity }: { swatch: PaletteSwatch; opacity: number }) {
  return (
    <>
      <div className="size-1.5 rounded-full" style={{ backgroundColor: swatch.accent }} />
      <div className="h-1 w-3/5 rounded-full" style={{ backgroundColor: swatch.text, opacity }} />
    </>
  );
}

function PreviewSidebarRow({ swatch }: { swatch: PaletteSwatch }) {
  return (
    <div className="flex h-2.5 shrink-0 items-center gap-1 px-1">
      <PreviewRowMarks swatch={swatch} opacity={0.25} />
    </div>
  );
}

/** A session row slid in the swipe direction, revealing the sidebar's action tile. */
function PreviewSwipedRow({
  swatch,
  direction,
  action,
}: {
  swatch: PaletteSwatch;
  direction: SwipeDirection;
  action: SwipeAction;
}) {
  const offset = action === "none" ? "0%" : direction === "right" ? "40%" : "-40%";
  return (
    <div className="relative h-5 shrink-0 overflow-hidden rounded-md">
      {action !== "none" && (
        <div
          className={cn(
            "absolute inset-y-0 flex w-2/5 items-center justify-center rounded-md",
            direction === "right" ? "left-0" : "right-0",
            action === "delete"
              ? "bg-destructive/20 text-destructive"
              : "bg-accent text-accent-foreground",
          )}
        >
          {action === "delete" ? (
            <Trash2Icon className="size-3" />
          ) : (
            <ArchiveIcon className="size-3" />
          )}
        </div>
      )}
      <div
        className="absolute inset-0 flex items-center gap-1 rounded-md px-1"
        style={{
          backgroundColor: swatch.bg,
          border: `1px solid ${swatch.border}`,
          transform: `translateX(${offset})`,
        }}
      >
        <PreviewRowMarks swatch={swatch} opacity={0.35} />
      </div>
    </div>
  );
}

/** One swipe direction's action on a mini sidebar; None leaves the row at rest. */
export function SwipeActionPreview({
  swatch,
  direction,
  action,
}: {
  swatch: PaletteSwatch;
  direction: SwipeDirection;
  action: SwipeAction;
}) {
  return (
    <div
      aria-hidden
      className="flex h-16 w-full overflow-hidden rounded-lg p-1.5"
      style={{ backgroundColor: swatch.bg, border: `1px solid ${swatch.border}` }}
    >
      <div
        className="flex flex-1 flex-col justify-center gap-1 rounded-md px-1"
        style={{ backgroundColor: swatch.card, border: `1px solid ${swatch.border}` }}
      >
        <PreviewSidebarRow swatch={swatch} />
        <PreviewSwipedRow swatch={swatch} direction={direction} action={action} />
        <PreviewSidebarRow swatch={swatch} />
      </div>
    </div>
  );
}
