// Project appearance: a project is marked by an emoji or a palette color, never
// both. Exports:
//   - `EmojiPicker`: a themed emoji-mart picker, code-split so its ~600KB
//     dataset never lands in the main bundle (loaded on first open).
//   - `ProjectAppearanceButton`: the mark tile beside a project name field,
//     opening the color / emoji picker on click (create and rename dialogs).
//   - `ProjectAppearanceTile`: the preview of a project's mark.
//   - `ProjectLandingIcon`: the big project-header icon on the new-chat landing,
//     whose picker applies a new mark immediately.

import {
  type CSSProperties,
  type RefObject,
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { CheckIcon, FolderIcon, Loader2Icon, PencilIcon, SmilePlusIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { Popover, PopoverAnchor, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useQuery } from "@tanstack/react-query";
import { useUpdateProjectConfig } from "@/hooks/useConversations";
import { readableForeground } from "@/lib/customTheme";
import {
  PROJECT_COLORS,
  type ProjectAppearance,
  isProjectColor,
  projectAppearance,
  projectColorValue,
  withProjectAppearance,
} from "@/lib/projectColors";
import { getRecommendedProjectColor, type ProjectConfig } from "@/lib/projectsApi";
import { cn } from "@/lib/utils";

// Both the picker component and its dataset are dynamically imported so they
// stay out of the initial bundle and off the static graph (tests that render a
// closed picker never touch the emoji JSON, which Node refuses under vitest).
const Picker = lazy(() => import("@emoji-mart/react"));
const loadEmojiData = async () => (await import("@emoji-mart/data")).default;

const TILE_SIZES = {
  sm: { tile: "size-[38px]", emoji: "text-xl", folder: "size-4" },
  lg: { tile: "size-14 rounded-xl", emoji: "text-[30px]", folder: "size-6" },
};

/** A themed emoji-mart picker. Calls `onSelect` with the chosen unicode glyph. */
export function EmojiPicker({ onSelect }: { onSelect: (native: string) => void }) {
  const mode = useResolvedThemeMode();
  return (
    <Suspense fallback={<div className="h-[420px] w-[352px]" aria-hidden />}>
      <Picker
        data={loadEmojiData}
        onEmojiSelect={(emoji: { native: string }) => onSelect(emoji.native)}
        theme={mode}
        navPosition="top"
        previewPosition="none"
        skinTonePosition="none"
        maxFrequentRows={2}
        perLine={8}
        autoFocus
      />
    </Suspense>
  );
}

/**
 * The server's recommended color for a new project: the one most different
 * from the colors the caller's projects use. Undefined until it loads; keyed
 * under `projects` so every project-list refresh refreshes it too.
 */
export function useRecommendedProjectColor(): string | undefined {
  const { data } = useQuery({
    queryKey: ["projects", "recommended-color"],
    queryFn: () => getRecommendedProjectColor(),
    staleTime: 30_000,
  });
  return isProjectColor(data) ? data : undefined;
}

/** Preview of a project's mark: the emoji, else a folder in its color. */
export function ProjectAppearanceTile({
  appearance,
  size,
  className,
}: {
  appearance: ProjectAppearance;
  size: keyof typeof TILE_SIZES;
  className?: string;
}) {
  const color = projectColorValue(appearance.color, useResolvedThemeMode());
  const sizes = TILE_SIZES[size];
  return (
    <span
      data-testid="project-appearance-tile"
      className={cn(
        "flex shrink-0 items-center justify-center",
        sizes.tile,
        appearance.icon ? "bg-muted" : "bg-tag-pink",
        className,
      )}
      style={
        !appearance.icon && color
          ? { backgroundColor: `color-mix(in oklab, ${color} 12%, transparent)` }
          : undefined
      }
    >
      {appearance.icon ? (
        <span className={cn("leading-none", sizes.emoji)}>{appearance.icon}</span>
      ) : (
        <FolderIcon className={cn("text-brand-accent", sizes.folder)} style={{ color }} />
      )}
    </span>
  );
}

/**
 * Popover body for picking a project's mark: an emoji button that swaps in
 * the emoji grid, then the palette swatches. Picking either replaces the other and
 * closes the popover via `onClose`.
 */
function ProjectAppearancePopoverContent({
  value,
  onChange,
  onClose,
  triggerRef,
  disabled,
  align,
}: {
  value: ProjectAppearance;
  onChange: (appearance: ProjectAppearance) => void;
  onClose: () => void;
  /** The control that opened the popover; a click on it is not "outside". */
  triggerRef: RefObject<HTMLElement | null>;
  disabled: boolean;
  align: "start" | "center";
}) {
  const mode = useResolvedThemeMode();
  const [showEmoji, setShowEmoji] = useState(false);
  // emoji-mart preventDefaults pointer events, so Radix never sees a click
  // elsewhere (e.g. in an enclosing dialog) as outside; close on it ourselves.
  useEffect(() => {
    const closeOnOutsidePointer = (event: PointerEvent) => {
      const target = event.target as Element | null;
      if (target?.closest('[data-slot="popover-content"]')) return;
      if (triggerRef.current?.contains(target)) return;
      onClose();
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer, true);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer, true);
  }, [onClose, triggerRef]);
  const pick = (next: ProjectAppearance) => {
    onChange(next);
    onClose();
  };

  return (
    <PopoverContent
      align={align}
      // Publish the collision-aware available viewport height (capped at the
      // picker's natural size) so the .emoji-picker-popover rule in index.css
      // shrinks emoji-mart to fit and scrolls it internally.
      collisionPadding={8}
      style={
        {
          "--emoji-picker-height": "min(420px, var(--radix-popover-content-available-height))",
        } as CSSProperties
      }
      className={cn(
        "emoji-picker-popover w-auto",
        showEmoji &&
          "flex max-h-[var(--radix-popover-content-available-height)] flex-col overflow-hidden p-0",
      )}
      // A Dialog's scroll lock preventDefaults wheel events over the emoji
      // grid's shadow DOM; stop them reaching it so the grid scrolls.
      onWheel={(event) => event.stopPropagation()}
    >
      {showEmoji ? (
        <EmojiPicker onSelect={(native) => pick({ icon: native, color: null })} />
      ) : (
        <div className="flex items-center gap-2" data-testid="project-appearance">
          <Button
            type="button"
            variant={value.icon ? "secondary" : "ghost"}
            size="icon-sm"
            aria-label="Choose an emoji"
            aria-pressed={Boolean(value.icon)}
            data-testid="project-emoji-button"
            disabled={disabled}
            onClick={() => setShowEmoji(true)}
          >
            {value.icon ? (
              <span className="text-base leading-none">{value.icon}</span>
            ) : (
              <SmilePlusIcon className="size-4" />
            )}
          </Button>
          <span aria-hidden className="h-5 w-px bg-border" />
          <div role="radiogroup" aria-label="Project color" className="flex gap-2">
            {PROJECT_COLORS.map((option) => {
              const checked = !value.icon && value.color === option.id;
              return (
                <button
                  key={option.id}
                  type="button"
                  role="radio"
                  aria-checked={checked}
                  aria-label={option.label}
                  data-testid={`project-color-${option.id}`}
                  disabled={disabled}
                  onClick={() => pick({ icon: null, color: option.id })}
                  className={cn(
                    "flex size-6 items-center justify-center rounded-full ring-1 ring-foreground/10 outline-none transition-shadow focus-visible:ring-2 focus-visible:ring-ring/40 disabled:cursor-not-allowed disabled:opacity-50",
                    checked && "ring-2 ring-foreground/40 ring-offset-2 ring-offset-background",
                  )}
                  style={{ backgroundColor: option[mode] }}
                >
                  {checked && (
                    <CheckIcon
                      className="size-3.5"
                      style={{ color: readableForeground(option[mode]) }}
                    />
                  )}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </PopoverContent>
  );
}

/**
 * The mark tile beside a project name field; clicking it opens the color /
 * emoji picker. Used by the New project and Rename project dialogs.
 */
export function ProjectAppearanceButton({
  value,
  onChange,
  disabled = false,
  testId,
}: {
  value: ProjectAppearance;
  onChange: (appearance: ProjectAppearance) => void;
  disabled?: boolean;
  testId: string;
}) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const close = useCallback(() => setOpen(false), []);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          ref={triggerRef}
          type="button"
          aria-label="Change project icon"
          data-testid={testId}
          disabled={disabled}
          className="shrink-0 cursor-pointer outline-none disabled:cursor-default disabled:opacity-50"
        >
          <ProjectAppearanceTile appearance={value} size="sm" />
        </button>
      </PopoverTrigger>
      {open && (
        <ProjectAppearancePopoverContent
          value={value}
          onChange={onChange}
          onClose={close}
          triggerRef={triggerRef}
          disabled={disabled}
          align="start"
        />
      )}
    </Popover>
  );
}

/**
 * The project-header icon on the new-chat landing. Its hover pencil opens the
 * appearance picker and saves each pick immediately, merging onto the
 * project's full `config` so the other stored defaults survive. A label-only
 * folder (`projectId === null`) is promoted on demand by the mutation.
 *
 * `configReady` gates editing: the PATCH replaces the whole config blob, so a
 * write before the config has loaded would merge onto `{}` and silently wipe
 * those defaults. The caller passes `true` only once the config has resolved
 * (or when there's no first-class config to lose).
 */
export function ProjectLandingIcon({
  projectId,
  projectName,
  config,
  configReady,
}: {
  projectId: string | null;
  projectName: string;
  config: ProjectConfig | undefined;
  configReady: boolean;
}) {
  const [open, setOpen] = useState(false);
  const anchorRef = useRef<HTMLSpanElement>(null);
  const close = useCallback(() => setOpen(false), []);
  const update = useUpdateProjectConfig();
  const appearance = projectAppearance(config);

  const openPicker = () => {
    if (configReady) setOpen(true);
  };
  const save = (next: ProjectAppearance) => {
    if (!configReady) return;
    update.mutate({
      id: projectId,
      name: projectName,
      config: withProjectAppearance(config ?? {}, next),
    });
  };

  return (
    <span ref={anchorRef} className="group/icon relative flex h-16 shrink-0 items-center">
      {/* Edit affordance, revealed above the tile on hover. */}
      <div className="absolute -top-1 left-1/2 flex -translate-x-1/2 -translate-y-full items-center gap-0.5 rounded-lg bg-popover p-0.5 opacity-0 shadow-menu ring-1 ring-foreground/10 transition-opacity focus-within:opacity-100 group-hover/icon:opacity-100">
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label="Change project icon"
          data-testid="project-icon-edit"
          disabled={!configReady}
          onClick={openPicker}
        >
          <PencilIcon className="size-3.5" />
        </Button>
      </div>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverAnchor asChild>
          <button
            type="button"
            aria-label="Project icon"
            data-testid="project-icon-tile"
            onClick={openPicker}
            className="cursor-pointer rounded-xl"
          >
            {update.isPending ? (
              <span className="flex size-14 items-center justify-center rounded-xl bg-muted">
                <Loader2Icon className="size-6 animate-spin text-muted-foreground" />
              </span>
            ) : (
              <ProjectAppearanceTile appearance={appearance} size="lg" />
            )}
          </button>
        </PopoverAnchor>
        {open && (
          <ProjectAppearancePopoverContent
            value={appearance}
            onChange={save}
            onClose={close}
            triggerRef={anchorRef}
            disabled={!configReady}
            align="center"
          />
        )}
      </Popover>
    </span>
  );
}
