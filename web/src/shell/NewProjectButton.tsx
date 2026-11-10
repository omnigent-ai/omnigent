import { type CSSProperties, useEffect, useRef, useState } from "react";
import { ChevronRightIcon, FolderOpenIcon, PlusIcon, SmilePlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
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
import { useCreateHostDirectory } from "@/hooks/useHostFilesystem";
import type { ProjectConfig } from "@/lib/projectsApi";
import { joinPath } from "./WorkspacePicker";
import { WorkspacePickerDialog } from "./WorkspacePickerDialog";

interface WorkspaceSelection {
  hostId: string;
  path: string;
}

function isSinglePathComponent(name: string): boolean {
  return (
    name !== "." &&
    name !== ".." &&
    !name.includes("/") &&
    !name.includes("\\") &&
    !name.includes("\0")
  );
}

function friendlyDirectoryError(error: unknown): string {
  const message = error instanceof Error ? error.message.toLowerCase() : "";
  if (message.includes("already exists")) {
    return "That folder already exists. Choose a different folder.";
  }
  if (message.includes("permission denied") || message.includes("not your host")) {
    return "You can’t create folders there. Reconnect your host or choose another location.";
  }
  if (
    message.includes("host not found") ||
    message.includes("host is offline") ||
    message.includes("host unavailable") ||
    message.includes("disconnected")
  ) {
    return "The host is offline. Reconnect it and try again.";
  }
  if (message.includes("timeout") || message.includes("timed out")) {
    return "The host didn’t respond. Check its connection and try again.";
  }
  if (
    message.includes("not a directory") ||
    message.includes("parent is a file") ||
    message.includes("path must")
  ) {
    return "That folder can’t be created. Uncheck the box to choose another location.";
  }
  return "The folder couldn’t be created. Check the host and try again.";
}

function friendlyProjectError(error: unknown): string {
  const message = error instanceof Error ? error.message.toLowerCase() : "";
  if (message.includes("already exists")) {
    return "A project with this name already exists. Use a different name.";
  }
  if (message.includes("too long")) {
    return "The project name is too long. Use a shorter name.";
  }
  if (message.includes("unauthorized") || message.includes("sign in")) {
    return "Your session expired. Sign in and try again.";
  }
  if (
    message.includes("failed to fetch") ||
    message.includes("network") ||
    message.includes("connection")
  ) {
    return "The project couldn’t be created. Check your connection and try again.";
  }
  return "The project couldn’t be created. Try again.";
}

/**
 * "New project" control in the Projects group header. Opens a dialog that
 * creates an EMPTY first-class project (`POST /v1/projects`) — the capability
 * the legacy label model can't express. On success the new folder is expanded
 * (via `onCreated`) so the user can immediately file sessions into it.
 */
export function NewProjectButton({
  onCreated,
  hostId,
  hostsLoading,
}: {
  onCreated: (name: string) => void;
  hostId: string | null;
  hostsLoading: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [icon, setIcon] = useState<string | undefined>(undefined);
  const [emojiOpen, setEmojiOpen] = useState(false);
  const [dialogHostId, setDialogHostId] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState<WorkspaceSelection | null>(null);
  const [createdWorkspace, setCreatedWorkspace] = useState<
    | (WorkspaceSelection & {
        projectName: string;
      })
    | null
  >(null);
  const [workspacePickerOpen, setWorkspacePickerOpen] = useState(false);
  const [createWorkspaceFolder, setCreateWorkspaceFolder] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const submissionGeneration = useRef(0);
  const createProject = useCreateProject();
  const createDirectory = useCreateHostDirectory();
  const isPending = createProject.isPending || createDirectory.isPending;
  const folderCreationDisabled = isPending || (!hostsLoading && dialogHostId === null);

  useEffect(() => {
    if (open && dialogHostId === null && hostId !== null) {
      setDialogHostId(hostId);
    }
  }, [dialogHostId, hostId, open]);

  const submit = async () => {
    const trimmed = name.trim();
    if (trimmed === "" || isPending) return;
    const submission = ++submissionGeneration.current;
    setSubmitError(null);

    let selectedWorkspace = workspace;
    if (createWorkspaceFolder) {
      if (!isSinglePathComponent(trimmed)) {
        setSubmitError("Use a project name without slashes or dot segments to create a folder.");
        return;
      }
      if (dialogHostId === null) {
        setSubmitError("Connect a host before creating a working directory.");
        return;
      }
      if (createdWorkspace?.hostId === dialogHostId && createdWorkspace.projectName === trimmed) {
        selectedWorkspace = createdWorkspace;
      } else {
        try {
          const path = await createDirectory.mutateAsync({
            hostId: dialogHostId,
            path: joinPath("~", trimmed),
          });
          if (submission !== submissionGeneration.current) return;
          selectedWorkspace = { hostId: dialogHostId, path };
          setCreatedWorkspace({ ...selectedWorkspace, projectName: trimmed });
        } catch (error) {
          if (submission !== submissionGeneration.current) return;
          setSubmitError(friendlyDirectoryError(error));
          return;
        }
      }
    }

    if (submission !== submissionGeneration.current) return;
    const config: ProjectConfig = {};
    if (icon) config.icon = icon;
    if (selectedWorkspace) {
      config.host_id = selectedWorkspace.hostId;
      config.workspace = selectedWorkspace.path;
    }
    createProject.mutate(
      { name: trimmed, config: Object.keys(config).length > 0 ? config : undefined },
      {
        onSuccess: (project) => {
          if (submission !== submissionGeneration.current) return;
          submissionGeneration.current += 1;
          setOpen(false);
          setName("");
          setIcon(undefined);
          setDialogHostId(null);
          setWorkspace(null);
          setCreatedWorkspace(null);
          setWorkspacePickerOpen(false);
          setCreateWorkspaceFolder(false);
          setSubmitError(null);
          onCreated(project.name);
        },
      },
    );
  };

  const dialog = (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          submissionGeneration.current += 1;
        }
        setOpen(next);
        if (!next) {
          setEmojiOpen(false);
          setWorkspacePickerOpen(false);
        }
      }}
    >
      <DialogContent
        className="sm:max-w-lg"
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
            Create a project, then file sessions into it from a session's menu.
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
                className="flex w-[38px] shrink-0 cursor-pointer items-center justify-center self-stretch bg-primary/10 outline-none transition-colors"
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
            placeholder="Project name…"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void submit();
              }
            }}
          />
        </div>
        <Collapsible>
          <CollapsibleTrigger className="group flex w-full cursor-pointer items-center gap-1 rounded-sm text-left outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
            <span className="font-medium text-ui">
              Working directory{" "}
              <span className="font-normal text-muted-foreground">(optional)</span>
            </span>
            <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground transition-transform duration-200 ease-out group-data-[state=open]:rotate-90" />
          </CollapsibleTrigger>
          <CollapsibleContent className="new-project-working-directory-content mt-2 flex flex-col gap-1.5">
            <label
              htmlFor="new-project-create-workspace"
              className={`flex h-10 items-center gap-2 rounded-lg border border-input bg-transparent px-3 text-ui ${
                folderCreationDisabled ? "cursor-not-allowed opacity-50" : "cursor-pointer"
              }`}
            >
              <Checkbox
                id="new-project-create-workspace"
                checked={createWorkspaceFolder}
                onCheckedChange={(checked) => {
                  setCreateWorkspaceFolder(checked === true);
                  setSubmitError(null);
                }}
                disabled={folderCreationDisabled}
              />
              <span>Create a folder using the project name</span>
            </label>
            <button
              id="new-project-workspace"
              type="button"
              data-testid="new-project-workspace"
              aria-label={`Working directory: ${
                createWorkspaceFolder
                  ? joinPath("~", name.trim() || "Project name")
                  : workspace?.path || "Not selected"
              }`}
              onClick={() => setWorkspacePickerOpen(true)}
              disabled={createWorkspaceFolder || dialogHostId === null || isPending}
              className="flex h-10 w-full items-center justify-between gap-3 rounded-lg border border-input bg-transparent px-3 text-ui outline-none transition-colors hover:bg-muted/50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <span className="flex min-w-0 items-center gap-2">
                <FolderOpenIcon className="size-4 shrink-0 text-muted-foreground" />
                <span className="truncate">
                  {createWorkspaceFolder
                    ? joinPath("~", name.trim() || "Project name")
                    : workspace?.path || (hostsLoading ? "Loading hosts…" : "Choose a folder…")}
                </span>
              </span>
              <span className="shrink-0 text-muted-foreground text-sm">Browse</span>
            </button>
            <WorkspacePickerDialog
              open={workspacePickerOpen}
              onOpenChange={setWorkspacePickerOpen}
              hostId={dialogHostId}
              initialPath={workspace?.path}
              onConfirm={(path) => {
                if (dialogHostId !== null) {
                  setWorkspace({ hostId: dialogHostId, path });
                }
              }}
              description="Choose a default working directory for sessions in this project."
            />
          </CollapsibleContent>
        </Collapsible>
        {(submitError || createProject.isError) && (
          <p className="text-ui text-destructive" role="alert">
            {submitError ?? friendlyProjectError(createProject.error)}
          </p>
        )}
        <DialogFooter className="border-t-0 bg-transparent">
          <Button type="button" variant="ghost" onClick={() => setOpen(false)} disabled={isPending}>
            Cancel
          </Button>
          <Button
            type="button"
            data-testid="new-project-confirm"
            loading={isPending}
            disabled={name.trim() === "" || isPending}
            onClick={() => void submit()}
          >
            Create
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );

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
              submissionGeneration.current += 1;
              setName("");
              setIcon(undefined);
              setDialogHostId(hostId);
              setWorkspace(null);
              setCreatedWorkspace(null);
              setWorkspacePickerOpen(false);
              setCreateWorkspaceFolder(false);
              setSubmitError(null);
              setOpen(true);
            }}
          >
            <PlusIcon className="size-3.5" />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">New project</TooltipContent>
      </Tooltip>
      {dialog}
    </>
  );
}
