// A project is marked by an emoji or a palette color, never both. emoji-mart is
// code-split so its ~600KB dataset loads only on first open.

import { type CSSProperties, lazy, Suspense, useEffect, useRef, useState } from "react";
import { CheckIcon, FolderIcon, PencilIcon, SmilePlusIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useUpdateProjectConfig } from "@/hooks/useConversations";
import { readableForeground } from "@/lib/customTheme";
import {
  PROJECT_COLORS,
  type ProjectAppearance,
  projectAppearance,
  projectColorValue,
  withProjectAppearance,
} from "@/lib/projectColors";
import type { ProjectConfig } from "@/lib/projectsApi";
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
function EmojiPicker({ onSelect }: { onSelect: (native: string) => void }) {
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

function ProjectAppearanceTile({
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
 * The mark tile that opens the emoji / color picker. Pass `open` and
 * `onOpenChange` to also open it from elsewhere.
 */
export function ProjectAppearanceButton({
  value,
  onChange,
  disabled = false,
  size = "sm",
  testId,
  open: controlledOpen,
  onOpenChange,
}: {
  value: ProjectAppearance;
  onChange: (appearance: ProjectAppearance) => void;
  disabled?: boolean;
  size?: keyof typeof TILE_SIZES;
  testId: string;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  const mode = useResolvedThemeMode();
  const [uncontrolledOpen, setUncontrolledOpen] = useState(false);
  const [showEmoji, setShowEmoji] = useState(false);
  const open = controlledOpen ?? uncontrolledOpen;
  const setOpen = (next: boolean) => {
    if (!next) setShowEmoji(false);
    setUncontrolledOpen(next);
    onOpenChange?.(next);
  };
  const pick = (next: ProjectAppearance) => {
    onChange(next);
    setOpen(false);
  };
  // Inside a modal Dialog, Radix doesn't report a click elsewhere in the
  // dialog as outside the popover, so close on it ourselves.
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef(() => setOpen(false));
  closeRef.current = () => setOpen(false);
  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      const target = event.target as Element | null;
      if (target?.closest('[data-slot="popover-content"]')) return;
      if (triggerRef.current?.contains(target)) return;
      closeRef.current();
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer, true);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer, true);
  }, [open]);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          ref={triggerRef}
          type="button"
          aria-label="Change project icon"
          data-testid={testId}
          disabled={disabled}
          className={cn(
            "shrink-0 cursor-pointer outline-none disabled:cursor-default",
            size === "lg" ? "rounded-xl" : "disabled:opacity-50",
          )}
        >
          <ProjectAppearanceTile appearance={value} size={size} />
        </button>
      </PopoverTrigger>
      <PopoverContent
        align={size === "lg" ? "center" : "start"}
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
  const update = useUpdateProjectConfig();
  // Show the picked mark while its save is in flight, so the tile changes once.
  const appearance = projectAppearance(update.isPending ? update.variables.config : config);

  const save = (next: ProjectAppearance) => {
    if (!configReady) return;
    update.mutate({
      id: projectId,
      name: projectName,
      config: withProjectAppearance(config ?? {}, next),
    });
  };

  return (
    <span className="group/icon relative flex h-16 shrink-0 items-center">
      {/* Edit affordance, revealed above the tile on hover. */}
      <div className="absolute -top-1 left-1/2 flex -translate-x-1/2 -translate-y-full items-center gap-0.5 rounded-lg bg-popover p-0.5 opacity-0 shadow-menu ring-1 ring-foreground/10 transition-opacity focus-within:opacity-100 group-hover/icon:opacity-100">
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label="Change project icon"
          data-testid="project-icon-edit"
          disabled={!configReady}
          onClick={() => setOpen(true)}
        >
          <PencilIcon className="size-3.5" />
        </Button>
      </div>
      <ProjectAppearanceButton
        value={appearance}
        onChange={save}
        disabled={!configReady}
        size="lg"
        testId="project-icon-tile"
        open={open}
        onOpenChange={setOpen}
      />
    </span>
  );
}
