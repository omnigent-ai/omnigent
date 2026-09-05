import { type CSSProperties, useState } from "react";
import { PlusIcon, SmilePlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { EmojiPicker } from "@/components/ProjectIconPicker";
import { useCreateProject } from "@/hooks/useConversations";
import { cn } from "@/lib/utils";

/**
 * "New project" control in the Projects group header. Opens a dialog that
 * creates an EMPTY first-class project (`POST /v1/projects`) — the capability
 * the legacy label model can't express. On success the new folder is expanded
 * (via `onCreated`) so the user can immediately file sessions into it.
 */
export function NewProjectButton({ onCreated }: { onCreated: (name: string) => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [icon, setIcon] = useState<string | undefined>(undefined);
  const [emojiOpen, setEmojiOpen] = useState(false);
  const createProject = useCreateProject();

  const submit = () => {
    const trimmed = name.trim();
    if (trimmed === "") return;
    createProject.mutate(
      { name: trimmed, icon },
      {
        onSuccess: (project) => {
          setOpen(false);
          setName("");
          setIcon(undefined);
          onCreated(project.name);
        },
      },
    );
  };

  return (
    <>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="New project"
            data-testid="new-project"
            className="text-muted-foreground"
            onClick={(e) => {
              e.stopPropagation();
              setName("");
              setIcon(undefined);
              setOpen(true);
            }}
          >
            <PlusIcon className="size-3.5" />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">New project</TooltipContent>
      </Tooltip>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent
          onClick={(e) => e.stopPropagation()}
          // emoji-mart preventDefaults the pointer event, so Radix's own
          // outside-dismissal never fires for clicks elsewhere in the modal.
          // Catch them in the capture phase and close the picker ourselves,
          // unless the pointer is inside the picker or on its trigger tile.
          onPointerDownCapture={(e) => {
            if (!emojiOpen) return;
            const target = e.target as Element;
            if (
              target.closest('[data-slot="popover-content"]') ||
              target.closest('[data-testid="new-project-icon"]')
            )
              return;
            setEmojiOpen(false);
          }}
        >
          <DialogHeader>
            <DialogTitle>New project</DialogTitle>
            <DialogDescription>
              Create an empty project, then file sessions into it from a session's menu.
            </DialogDescription>
          </DialogHeader>
          {/* One combined control: emoji tile (left) + name (right) share a
              single border, so it reads as a single input. */}
          <div className="flex items-stretch overflow-hidden rounded-lg border border-input">
            <Popover open={emojiOpen} onOpenChange={setEmojiOpen}>
              <PopoverTrigger asChild>
                <button
                  type="button"
                  aria-label="Choose project icon"
                  data-testid="new-project-icon"
                  className={cn(
                    "flex size-[38px] shrink-0 cursor-pointer items-center justify-center outline-none transition-colors",
                    icon ? "bg-muted" : "bg-tag-pink",
                  )}
                >
                  {icon ? (
                    <span className="text-xl leading-none">{icon}</span>
                  ) : (
                    <SmilePlusIcon className="size-4 text-brand-accent" />
                  )}
                </button>
              </PopoverTrigger>
              <PopoverContent
                align="start"
                // Publish the collision-aware available viewport height (Radix
                // exposes it as a CSS var), capped at the picker's natural size,
                // so the .emoji-picker-popover rule in index.css shrinks
                // emoji-mart to fit — it then scrolls its grid internally
                // instead of clipping on short screens.
                collisionPadding={8}
                style={
                  {
                    "--emoji-picker-height":
                      "min(420px, var(--radix-popover-content-available-height))",
                  } as CSSProperties
                }
                className="emoji-picker-popover flex max-h-[var(--radix-popover-content-available-height)] w-auto flex-col overflow-hidden p-0"
                // The Dialog's scroll lock (react-remove-scroll) preventDefaults
                // wheel events over the picker — it can't see emoji-mart's scroll
                // region inside shadow DOM. Stop the wheel from reaching the
                // document-level lock so the grid scrolls.
                onWheel={(e) => e.stopPropagation()}
                onInteractOutside={() => setEmojiOpen(false)}
              >
                <EmojiPicker
                  onSelect={(native) => {
                    setIcon(native);
                    setEmojiOpen(false);
                  }}
                />
              </PopoverContent>
            </Popover>
            <input
              autoFocus
              className="w-full bg-transparent px-3 py-2 text-ui outline-none"
              placeholder="Project name…"
              value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  submit();
                }
              }}
            />
          </div>
          {createProject.isError && (
            <p className="text-ui text-destructive" role="alert">
              {(createProject.error as Error).message}
            </p>
          )}
          <DialogFooter className="border-t-0 bg-transparent">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setOpen(false)}
              disabled={createProject.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              data-testid="new-project-confirm"
              loading={createProject.isPending}
              disabled={name.trim() === ""}
              onClick={submit}
            >
              Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
