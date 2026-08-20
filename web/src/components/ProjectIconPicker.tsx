// Emoji icon picker for projects. Two exports:
//   - `EmojiPicker`: a themed emoji-mart picker, code-split so its ~600KB
//     dataset never lands in the main bundle (loaded on first open).
//   - `ProjectLandingIcon`: the big project-header icon on the new-chat landing
//     — pink folder by default, the chosen emoji in a gray tile once set, with
//     hover-revealed edit/remove affordances (the OMNI-3742 design).

import { lazy, Suspense, useState } from "react";
import { useTheme } from "next-themes";
import { FolderIcon, PencilIcon, Trash2Icon } from "lucide-react";

import { Button } from "@/components/ui/button";
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
  const { resolvedTheme } = useTheme();
  return (
    <Suspense fallback={<div className="h-[420px] w-[352px]" aria-hidden />}>
      <Picker
        data={loadEmojiData}
        onEmojiSelect={(emoji: { native: string }) => onSelect(emoji.native)}
        theme={resolvedTheme === "dark" ? "dark" : "light"}
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
 * The project-header icon on the new-chat landing. Reads/writes the emoji
 * through the project's `config.icon`, merging so the other stored defaults
 * (host / workspace / agent) survive an icon change or removal. A label-only
 * folder (`projectId === null`) is promoted on demand by the mutation.
 */
export function ProjectLandingIcon({
  projectId,
  projectName,
  config,
}: {
  projectId: string | null;
  projectName: string;
  config: ProjectConfig | undefined;
}) {
  const [open, setOpen] = useState(false);
  const update = useUpdateProjectConfig();
  const icon = config?.icon;

  const save = (native: string) => {
    update.mutate({
      id: projectId,
      name: projectName,
      config: { ...(config ?? {}), icon: native },
    });
    setOpen(false);
  };
  const clear = () => {
    const next = { ...(config ?? {}) };
    delete next.icon;
    update.mutate({ id: projectId, name: projectName, config: next });
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
          onClick={() => setOpen(true)}
        >
          <PencilIcon className="size-3.5" />
        </Button>
        {icon ? (
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="Remove project icon"
            onClick={clear}
          >
            <Trash2Icon className="size-3.5" />
          </Button>
        ) : null}
      </div>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverAnchor asChild>
          <button
            type="button"
            aria-label="Change project icon"
            onClick={() => setOpen((o) => !o)}
            className={cn(
              "flex size-14 items-center justify-center rounded-xl transition-colors",
              icon ? "bg-muted" : "bg-tag-pink",
            )}
          >
            {icon ? (
              <span className="text-[30px] leading-none">{icon}</span>
            ) : (
              <FolderIcon className="size-6 text-brand-accent" />
            )}
          </button>
        </PopoverAnchor>
        <PopoverContent
          align="center"
          className="w-auto border-0 bg-transparent p-0 shadow-none ring-0"
        >
          <EmojiPicker onSelect={save} />
        </PopoverContent>
      </Popover>
    </span>
  );
}
