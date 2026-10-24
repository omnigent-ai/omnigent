import type { TerminalExtraKeysMode } from "@/lib/terminalExtraKeysPreferences";
import type { PaletteSwatch } from "@/lib/themePalette";
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

/** System's "/" split: the top layer shows only right of the diagonal. */
const SPLIT_CLIP_PATH = "polygon(62% 0, 100% 0, 100% 100%, 38% 100%)";

export function ModePreview({ variant }: { variant: ThemeMode }) {
  if (variant === "light") return <PaletteSwatchPreview swatch={LIGHT_MODE_PREVIEW} />;
  if (variant === "dark") return <PaletteSwatchPreview swatch={DARK_MODE_PREVIEW} />;
  return (
    <div className="relative h-16 w-full">
      <PaletteSwatchPreview swatch={LIGHT_MODE_PREVIEW} />
      <div aria-hidden className="absolute inset-0" style={{ clipPath: SPLIT_CLIP_PATH }}>
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

function ExtraKeysScene({ swatch, keysRow }: { swatch: PaletteSwatch; keysRow: boolean }) {
  return (
    <div
      aria-hidden
      className="flex h-16 w-full flex-col gap-1 overflow-hidden rounded-lg p-1.5"
      style={{ backgroundColor: swatch.bg, border: `1px solid ${swatch.border}` }}
    >
      <div
        className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-hidden rounded-md p-1"
        style={{ backgroundColor: swatch.card, border: `1px solid ${swatch.border}` }}
      >
        <div
          className="h-1 w-3/5 shrink-0 rounded-full"
          style={{ backgroundColor: swatch.text, opacity: 0.5 }}
        />
        <div
          className="h-1 w-2/5 shrink-0 rounded-full"
          style={{ backgroundColor: swatch.text, opacity: 0.3 }}
        />
        {!keysRow && (
          <div
            className="h-1 w-1/2 shrink-0 rounded-full"
            style={{ backgroundColor: swatch.text, opacity: 0.3 }}
          />
        )}
      </div>
      {keysRow && (
        <div data-testid="extra-keys-preview-pills" className="flex shrink-0 gap-0.5">
          {Array.from({ length: 7 }, (_, index) => (
            <div
              key={index}
              className="h-1.5 flex-1 rounded-sm"
              style={{ backgroundColor: swatch.text, opacity: 0.5 }}
            />
          ))}
        </div>
      )}
      <div
        className="grid shrink-0 grid-cols-10 gap-0.5 rounded-md p-0.5"
        style={{ backgroundColor: swatch.card, border: `1px solid ${swatch.border}` }}
      >
        {Array.from({ length: 20 }, (_, index) => (
          <div
            key={index}
            className="h-1 rounded-sm"
            style={{ backgroundColor: swatch.text, opacity: 0.2 }}
          />
        ))}
      </div>
    </div>
  );
}

/**
 * Terminal over an on-screen keyboard, with (Always) or without (Never) the
 * extra-keys row. Auto splits Always / Never on System's diagonal.
 */
export function ExtraKeysPreview({
  mode,
  swatch,
}: {
  mode: TerminalExtraKeysMode;
  swatch: PaletteSwatch;
}) {
  if (mode !== "auto") return <ExtraKeysScene swatch={swatch} keysRow={mode === "on"} />;
  return (
    // One grid cell for both layers so the frames, terminal and keyboard line up.
    <div data-testid="extra-keys-preview-auto" className="grid h-16 w-full">
      <div className="col-start-1 row-start-1">
        <ExtraKeysScene swatch={swatch} keysRow />
      </div>
      <div aria-hidden className="col-start-1 row-start-1" style={{ clipPath: SPLIT_CLIP_PATH }}>
        <ExtraKeysScene swatch={swatch} keysRow={false} />
      </div>
    </div>
  );
}
