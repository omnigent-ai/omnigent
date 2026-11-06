import { type CSSProperties, useEffect, useState } from "react";
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
import { readLastCloneParent, useCloneHostRepository } from "@/hooks/useCloneHostRepository";
import { useCreateProject } from "@/hooks/useConversations";
import { useHosts } from "@/hooks/useHosts";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { deriveRepoName } from "@/lib/sandboxRepositories";
import { CloneRepositoryFields, cloneDestination } from "./CloneRepository";

/**
 * "New project" control in the Projects group header. Creates a project,
 * optionally cloning a repository as its default workspace, then expands it
 * via `onCreated`.
 */
export function NewProjectButton({ onCreated }: { onCreated: (name: string) => void }) {
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [emojiOpen, setEmojiOpen] = useState(false);

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
              setEmojiOpen(false);
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
          {/* Mounted only while open, so each open starts fresh and a closed
              dialog keeps no hosts or mutation observers. */}
          <NewProjectForm
            emojiOpen={emojiOpen}
            onEmojiOpenChange={setEmojiOpen}
            onPendingChange={setPending}
            onCancel={() => setOpen(false)}
            onCreated={(createdName) => {
              setOpen(false);
              onCreated(createdName);
            }}
          />
        </DialogContent>
      </Dialog>
    </>
  );
}

/** The New project form; it reports `pending` so the dialog cannot close mid-clone. */
function NewProjectForm({
  emojiOpen,
  onEmojiOpenChange: setEmojiOpen,
  onPendingChange,
  onCancel,
  onCreated,
}: {
  emojiOpen: boolean;
  onEmojiOpenChange: (open: boolean) => void;
  onPendingChange: (pending: boolean) => void;
  onCancel: () => void;
  onCreated: (name: string) => void;
}) {
  const [name, setName] = useState("");
  const [icon, setIcon] = useState<string | undefined>(undefined);
  const [cloneOpen, setCloneOpen] = useState(false);
  const [cloneHostId, setCloneHostId] = useState<string | null>(null);
  const [cloneUrl, setCloneUrl] = useState("");
  // ``null`` until the user edits the folder; until then it follows the host.
  const [cloneParentDraft, setCloneParentDraft] = useState<string | null>(null);
  const createProject = useCreateProject();
  const cloneRepository = useCloneHostRepository();
  const info = useServerInfo();
  const cloneSupported = info !== "loading" && info.host_repository_clone === true;
  const { data: hosts } = useHosts({ enabled: cloneSupported });
  // An explicitly chosen host stays listed while offline, so it is never swapped silently.
  const cloneHosts = (hosts ?? []).filter(
    (host) =>
      host.sandbox_provider == null && (host.status === "online" || host.host_id === cloneHostId),
  );
  const selectedCloneHostId =
    cloneHostId ?? cloneHosts.find((host) => host.status === "online")?.host_id ?? null;
  const selectedCloneHost = cloneHosts.find((host) => host.host_id === selectedCloneHostId);
  const selectedCloneHostOnline = selectedCloneHost?.status === "online";
  const cloneParent = cloneParentDraft ?? readLastCloneParent(selectedCloneHostId);
  const cloneRequested = cloneOpen && cloneUrl.trim() !== "";
  const cloneReady =
    cloneRequested &&
    selectedCloneHostId !== null &&
    selectedCloneHostOnline &&
    cloneDestination(cloneUrl, cloneParent) !== null;
  const projectName = name.trim() || (cloneReady ? (deriveRepoName(cloneUrl) ?? "") : "");
  const pending = createProject.isPending || cloneRepository.isPending;
  const canSubmit = projectName !== "" && (!cloneRequested || cloneReady) && !pending;

  useEffect(() => {
    onPendingChange(pending);
  }, [onPendingChange, pending]);
  useEffect(() => () => onPendingChange(false), [onPendingChange]);

  const submit = async () => {
    if (!canSubmit) return;
    if (!cloneRequested || selectedCloneHostId === null) {
      createProject.mutate({ name: projectName, icon }, { onSuccess: (p) => onCreated(p.name) });
      return;
    }
    // Pin the host so a retry stays on it even if another host becomes the default.
    setCloneHostId(selectedCloneHostId);
    let workspace: string;
    try {
      ({ path: workspace } = await cloneRepository.mutateAsync({
        hostId: selectedCloneHostId,
        url: cloneUrl,
        parentDirectory: cloneParent,
      }));
    } catch {
      return;
    }
    createProject.mutate(
      { name: projectName, icon, config: { host_id: selectedCloneHostId, workspace } },
      { onSuccess: (p) => onCreated(p.name) },
    );
  };

  const toggleCloneSection = () => {
    setCloneOpen(!cloneOpen);
    setCloneParentDraft(null);
    cloneRepository.reset();
  };

  return (
    <>
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
      {cloneSupported && (
        <div className="flex flex-col gap-3">
          <button
            type="button"
            className="flex w-fit items-center gap-1 text-sm font-medium text-muted-foreground hover:text-foreground"
            aria-expanded={cloneOpen}
            data-testid="new-project-clone-toggle"
            disabled={pending}
            onClick={toggleCloneSection}
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
                      setCloneParentDraft(null);
                      cloneRepository.reset();
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
                          {host.status === "online" ? host.name : `${host.name} (offline)`}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
                {cloneHostId !== null && hosts !== undefined && !selectedCloneHost && (
                  <p
                    className="text-sm text-destructive"
                    data-testid="new-project-clone-host-missing"
                  >
                    The chosen host is no longer connected. Choose another host.
                  </p>
                )}
                {selectedCloneHost && !selectedCloneHostOnline && (
                  <p
                    className="text-sm text-destructive"
                    data-testid="new-project-clone-host-offline"
                  >
                    {selectedCloneHost.name} is offline. Reconnect it or choose another host.
                  </p>
                )}
              </div>
              <CloneRepositoryFields
                hostId={selectedCloneHostId}
                url={cloneUrl}
                onUrlChange={(url) => {
                  setCloneUrl(url);
                  cloneRepository.reset();
                }}
                parentDirectory={cloneParent}
                onParentDirectoryChange={setCloneParentDraft}
                onSubmit={() => void submit()}
                disabled={pending}
              />
            </div>
          )}
        </div>
      )}
      {cloneOpen && cloneRepository.isError && (
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
        <Button type="button" variant="ghost" onClick={onCancel} disabled={pending}>
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
    </>
  );
}
