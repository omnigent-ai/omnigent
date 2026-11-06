import { type CSSProperties, useState } from "react";
import { ChevronDownIcon, ChevronRightIcon, PlusIcon, SmilePlusIcon } from "lucide-react";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { EmojiPicker } from "@/components/ProjectIconPicker";
import { useCloneHostRepository } from "@/hooks/useCloneHostRepository";
import { useCreateProject } from "@/hooks/useConversations";
import { useHosts } from "@/hooks/useHosts";
import { deriveRepoName } from "@/lib/sandboxRepositories";
import {
  CloneRepositoryFields,
  cloneDestination,
  readLastCloneParent,
  rememberCloneParent,
} from "./CloneRepository";

/**
 * "New project" control in the Projects group header. Opens a dialog that
 * creates a first-class project (`POST /v1/projects`) — the capability the
 * legacy label model can't express. The optional clone section first clones a
 * repository onto a host and stores that checkout as the project's default
 * host and working directory. On success the new folder is expanded (via
 * `onCreated`) so the user can immediately file sessions into it.
 */
export function NewProjectButton({ onCreated }: { onCreated: (name: string) => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [icon, setIcon] = useState<string | undefined>(undefined);
  const [emojiOpen, setEmojiOpen] = useState(false);
  const [cloneOpen, setCloneOpen] = useState(false);
  const [cloneHostId, setCloneHostId] = useState<string | null>(null);
  const [cloneUrl, setCloneUrl] = useState("");
  const [cloneParent, setCloneParent] = useState("~");
  const createProject = useCreateProject();
  const cloneRepository = useCloneHostRepository();
  const { data: hosts } = useHosts({ enabled: open });
  const cloneHosts = (hosts ?? []).filter(
    (host) => host.status === "online" && host.sandbox_provider == null,
  );
  // Default to the first online host until the user picks one.
  const selectedCloneHostId =
    cloneHostId !== null && cloneHosts.some((host) => host.host_id === cloneHostId)
      ? cloneHostId
      : (cloneHosts[0]?.host_id ?? null);
  const cloneRequested = cloneOpen && cloneUrl.trim() !== "";
  const cloneReady =
    cloneRequested &&
    selectedCloneHostId !== null &&
    cloneDestination(cloneUrl, cloneParent) !== null;
  const projectName = name.trim() || (cloneReady ? (deriveRepoName(cloneUrl) ?? "") : "");
  const pending = createProject.isPending || cloneRepository.isPending;
  const canSubmit = projectName !== "" && (!cloneRequested || cloneReady) && !pending;

  const reset = () => {
    setName("");
    setIcon(undefined);
    setCloneOpen(false);
    setCloneUrl("");
    createProject.reset();
    cloneRepository.reset();
  };

  const finish = (createdName: string) => {
    setOpen(false);
    reset();
    onCreated(createdName);
  };

  const submit = async () => {
    if (!canSubmit) return;
    if (!cloneRequested || selectedCloneHostId === null) {
      createProject.mutate({ name: projectName, icon }, { onSuccess: (p) => finish(p.name) });
      return;
    }
    let workspace: string;
    try {
      ({ path: workspace } = await cloneRepository.mutateAsync({
        hostId: selectedCloneHostId,
        url: cloneUrl,
        parentDirectory: cloneParent,
      }));
    } catch {
      return; // Shown inline from the mutation's error state.
    }
    rememberCloneParent(selectedCloneHostId, cloneParent);
    createProject.mutate(
      { name: projectName, icon, config: { host_id: selectedCloneHostId, workspace } },
      { onSuccess: (p) => finish(p.name) },
    );
  };

  const openCloneSection = () => {
    const nextOpen = !cloneOpen;
    setCloneOpen(nextOpen);
    if (nextOpen) setCloneParent(readLastCloneParent(selectedCloneHostId));
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
            className="text-muted-foreground mobile-touch-target"
            onClick={(e) => {
              e.stopPropagation();
              reset();
              setOpen(true);
            }}
          >
            <PlusIcon className="size-3.5" />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">New project</TooltipContent>
      </Tooltip>
      <Dialog
        open={open}
        onOpenChange={(next) => {
          // Keep the dialog up while a clone runs so its result stays visible.
          if (!next && pending) return;
          setOpen(next);
        }}
      >
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
              Create a project, then file sessions into it from a session's menu. Optionally clone a
              repository to use as its working directory.
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
                  className="flex size-[38px] shrink-0 cursor-pointer items-center justify-center bg-primary/10 outline-none transition-colors"
                >
                  {icon ? (
                    <span className="text-xl leading-none">{icon}</span>
                  ) : (
                    <SmilePlusIcon className="size-4 text-primary" />
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
              placeholder={cloneReady ? `Project name (default: ${projectName})` : "Project name…"}
              data-testid="new-project-name"
              value={name}
              disabled={pending}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void submit();
                }
              }}
            />
          </div>
          <div className="flex flex-col gap-3">
            <button
              type="button"
              className="flex w-fit items-center gap-1 text-sm font-medium text-muted-foreground hover:text-foreground"
              aria-expanded={cloneOpen}
              data-testid="new-project-clone-toggle"
              disabled={pending}
              onClick={openCloneSection}
            >
              {cloneOpen ? (
                <ChevronDownIcon className="size-4" />
              ) : (
                <ChevronRightIcon className="size-4" />
              )}
              Clone a repository
            </button>
            {cloneOpen && (
              <div className="flex flex-col gap-3" data-testid="new-project-clone-section">
                <div className="flex flex-col gap-1.5">
                  <label className="text-sm font-medium text-muted-foreground">Host</label>
                  {cloneHosts.length === 0 ? (
                    <p className="text-sm text-muted-foreground" data-testid="new-project-no-host">
                      Connect a host to clone a repository onto it.
                    </p>
                  ) : (
                    <Select
                      value={selectedCloneHostId ?? undefined}
                      onValueChange={(hostId) => {
                        setCloneHostId(hostId);
                        setCloneParent(readLastCloneParent(hostId));
                      }}
                      disabled={pending}
                    >
                      <SelectTrigger
                        aria-label="Host"
                        data-testid="new-project-clone-host"
                        className="w-full"
                      >
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {cloneHosts.map((host) => (
                          <SelectItem key={host.host_id} value={host.host_id}>
                            {host.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                </div>
                <CloneRepositoryFields
                  hostId={selectedCloneHostId}
                  url={cloneUrl}
                  onUrlChange={setCloneUrl}
                  parentDirectory={cloneParent}
                  onParentDirectoryChange={setCloneParent}
                  onSubmit={() => void submit()}
                  disabled={pending}
                />
              </div>
            )}
          </div>
          {cloneRepository.isError && (
            <p
              className="text-ui break-words text-destructive"
              role="alert"
              data-testid="new-project-clone-error"
            >
              {cloneRepository.error.message}
            </p>
          )}
          {createProject.isError && (
            <p className="text-ui text-destructive" role="alert">
              {(createProject.error as Error).message}
            </p>
          )}
          <DialogFooter className="border-t-0 bg-transparent">
            <Button type="button" variant="ghost" onClick={() => setOpen(false)} disabled={pending}>
              Cancel
            </Button>
            <Button
              type="button"
              data-testid="new-project-confirm"
              loading={pending}
              disabled={!canSubmit}
              onClick={() => void submit()}
            >
              {cloneRepository.isPending ? "Cloning…" : "Create"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
