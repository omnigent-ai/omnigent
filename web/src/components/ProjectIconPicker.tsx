// Emoji icon picker for projects. Three exports:
//   - `EmojiPicker`: a themed emoji-mart picker, code-split so its ~600KB
//     dataset never lands in the main bundle (loaded on first open).
//   - `ProjectIconControl`: the controlled project-icon tile — the chosen emoji
//     (or a pink folder default) in a tile, with hover-revealed edit / remove
//     affordances and the picker popover. Never persists on its own; the host
//     owns the write, so a form can stage the pick and commit it on submit.
//   - `ProjectLandingIcon`: the write-immediately wrapper for the new-chat
//     landing — drives `ProjectIconControl` with a project-config mutation.

import { type CSSProperties, lazy, Suspense, useState } from "react";
import { FolderIcon, Loader2Icon, PencilIcon, Trash2Icon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { Popover, PopoverAnchor, PopoverContent } from "@/components/ui/popover";
import { useUpdateProjectConfig } from "@/hooks/useConversations";
import type { ProjectConfig } from "@/lib/projectsApi";
import { cn } from "@/lib/utils";

// Both the picker component and its dataset are dynamically imported so they
// stay out of the initial bundle and off the static graph (tests that render a
// closed picker never touch the emoji JSON, which Node refuses under vitest).
const Picker = lazy(() => import("@emoji-mart/react"));
const loadEmojiData = async () => (await import("@emoji-mart/data")).default;

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
 * The project-icon tile: the chosen emoji in a gray tile (a pink folder by
 * default) with hover-revealed edit / remove affordances and an emoji-picker
 * popover. Fully controlled — it never persists on its own. Picking an emoji
 * calls `onChange(glyph)`; the trash button calls `onChange(undefined)`. The
 * host decides when, or whether, to write: a form can stage the pick in local
 * state and emit it in a single submit — so Cancel discards it and no write
 * races the form's own save — while the landing page wires `onChange` straight
 * to a mutation.
 *
 * `pending` shows the busy spinner on the tile. `disabled` gates editing: a
 * host whose backing config hasn't loaded passes `true`, since a write before
 * then would merge onto an empty blob and wipe the stored defaults.
 */
export function ProjectIconControl({
  value,
  onChange,
  pending = false,
  disabled = false,
}: {
  value: string | undefined;
  onChange: (glyph: string | undefined) => void;
  pending?: boolean;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);

  const openPicker = () => {
    if (!disabled) setOpen(true);
  };

  return (
    <span className="group/icon relative flex h-16 shrink-0 items-center">
      {/* Edit / remove affordances, revealed above the tile on hover. Remove is
          hidden until an emoji is set (nothing to reset to). */}
      <div className="absolute -top-1 left-1/2 flex -translate-x-1/2 -translate-y-full items-center gap-0.5 rounded-lg bg-popover p-0.5 opacity-0 shadow-menu ring-1 ring-foreground/10 transition-opacity focus-within:opacity-100 group-hover/icon:opacity-100">
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label="Change project icon"
          data-testid="project-icon-edit"
          disabled={disabled}
          onClick={openPicker}
        >
          <PencilIcon className="size-3.5" />
        </Button>
        {value ? (
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="Remove project icon"
            data-testid="project-icon-remove"
            disabled={disabled}
            onClick={() => onChange(undefined)}
          >
            <Trash2Icon className="size-3.5" />
          </Button>
        ) : null}
      </div>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverAnchor asChild>
          <button
            type="button"
            aria-label="Project icon"
            data-testid="project-icon-tile"
            onClick={openPicker}
            className={cn(
              "flex size-14 cursor-pointer items-center justify-center rounded-xl transition-colors",
              value ? "bg-muted" : "bg-tag-pink",
            )}
          >
            {pending ? (
              <Loader2Icon
                className="size-6 animate-spin text-muted-foreground"
                data-testid="project-icon-pending"
              />
            ) : value ? (
              <span className="text-[30px] leading-none">{value}</span>
            ) : (
              <FolderIcon className="size-6 text-brand-accent" />
            )}
          </button>
        </PopoverAnchor>
        <PopoverContent
          align="center"
          // Publish the collision-aware available viewport height (capped at the
          // picker's natural size) so the .emoji-picker-popover rule in index.css
          // shrinks emoji-mart to fit and it scrolls internally on short screens.
          collisionPadding={8}
          style={
            {
              "--emoji-picker-height": "min(420px, var(--radix-popover-content-available-height))",
            } as CSSProperties
          }
          className="emoji-picker-popover w-auto border-0 bg-transparent p-0 shadow-none ring-0"
        >
          <EmojiPicker
            onSelect={(native) => {
              onChange(native);
              setOpen(false);
            }}
          />
        </PopoverContent>
      </Popover>
    </span>
  );
}

/**
 * The project-header icon on the new-chat landing, where there's no form around
 * it — edits persist immediately. Wraps {@link ProjectIconControl} with a
 * mutation that merges the change onto the project's stored `config` (so the
 * other defaults — host / workspace / agent — survive an icon change or
 * removal) and promotes a label-only folder (`projectId === null`) on demand.
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
  const update = useUpdateProjectConfig();

  const write = (glyph: string | undefined) => {
    if (!configReady) return;
    const next = { ...(config ?? {}) };
    if (glyph) next.icon = glyph;
    else delete next.icon;
    update.mutate({ id: projectId, name: projectName, config: next });
  };

  return (
    <ProjectIconControl
      value={config?.icon}
      onChange={write}
      pending={update.isPending}
      disabled={!configReady}
    />
  );
}
