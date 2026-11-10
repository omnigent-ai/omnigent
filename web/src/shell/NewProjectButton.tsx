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
import { useCreateProject, useProjects } from "@/hooks/useConversations";
import { useCloneHostRepository, useCreateHostDirectory } from "@/hooks/useHostFilesystem";
import type { ProjectConfig } from "@/lib/projectsApi";
import { useHosts, type Host } from "@/hooks/useHosts";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  sandboxOptionLabel,
  sandboxProviderOptions,
  sandboxRepositoryCapabilities,
} from "@/lib/capabilities";
import {
  readLastHostChoice,
  readLastSandboxProvider,
  SANDBOX_HOST_CHOICE,
  sandboxHostChoice,
  sandboxHostChoiceProvider,
} from "@/lib/hostPreferences";
import { deriveRepoName, repositorySelectionError } from "@/lib/sandboxRepositories";
import type { LastSandboxRepo } from "@/lib/repoPreferences";
import { ApiError } from "@/lib/sessionsApi";
import { SandboxRepositoryPicker } from "./SandboxRepositoryPicker";

import { isNavigablePath, joinPath } from "./WorkspacePicker";
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
  contextHost,
}: {
  onCreated: (name: string) => void;
  hostId: string | null;
  hostsLoading: boolean;
  contextHost?: Host;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [icon, setIcon] = useState<string | undefined>(undefined);
  const [emojiOpen, setEmojiOpen] = useState(false);
  const info = useServerInfo();
  const repositoryCreation = info !== "loading" && info.project_repository_creation === true;
  const { data: hosts = [] } = useHosts({ enabled: open && repositoryCreation });
  const { data: projects = [] } = useProjects({ enabled: open });
  const providers =
    info !== "loading" && info.managed_sandboxes_enabled ? sandboxProviderOptions(info) : [];
  const [targetChoice, setTargetChoice] = useState<string | null>(null);
  const targetEdited = useRef(false);
  const sandboxProvider =
    targetChoice === null ? undefined : sandboxHostChoiceProvider(targetChoice);
  const sandboxSelected = repositoryCreation && sandboxProvider !== undefined;
  const dialogHostId = sandboxSelected ? null : targetChoice;
  const [workspaceMode, setWorkspaceMode] = useState("folder");
  const [repositories, setRepositories] = useState<LastSandboxRepo[]>([]);
  const repository = repositories[0] ?? { url: "", branch: "" };
  const repositoryName = deriveRepoName(repository.url) ?? "repository";
  const [cloneDestination, setCloneDestination] = useState<string | null>(null);
  const destination = cloneDestination ?? joinPath("~", repositoryName);
  const [clonedWorkspace, setClonedWorkspace] = useState<{ key: string; path: string } | null>(
    null,
  );
  const [phase, setPhase] = useState<"cloning" | "creating" | null>(null);
  const submitting = useRef(false);
  const cloneRepository = useCloneHostRepository();
  const repositoryCapabilities = sandboxSelected
    ? sandboxRepositoryCapabilities(info, sandboxProvider ?? null)
    : { catalog: [], urlInput: true, branchSelection: true };
  const repositoryMode = repositoryCreation && (sandboxSelected || workspaceMode === "repository");
  const selectedHost = hosts.find((host) => host.host_id === dialogHostId);
  const initialTarget = () => {
    if (
      contextHost?.sandbox_provider &&
      repositoryCreation &&
      providers.includes(contextHost.sandbox_provider)
    ) {
      return sandboxHostChoice(contextHost.sandbox_provider);
    }
    if (contextHost?.status === "online" && !contextHost.sandbox_provider)
      return contextHost.host_id;
    const remembered = readLastHostChoice();
    if (repositoryCreation && remembered === SANDBOX_HOST_CHOICE && providers.length) {
      const provider = readLastSandboxProvider();
      return sandboxHostChoice(provider ?? providers[0]);
    }
    if (repositoryCreation && remembered && remembered !== SANDBOX_HOST_CHOICE) return remembered;
    return (
      hostId ?? (repositoryCreation && providers.length ? sandboxHostChoice(providers[0]) : null)
    );
  };
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
  const isPending =
    phase !== null ||
    createProject.isPending ||
    createDirectory.isPending ||
    cloneRepository.isPending;
  const folderCreationDisabled = isPending || (!hostsLoading && dialogHostId === null);

  useEffect(() => {
    if (open && !targetEdited.current && targetChoice === null && hostId !== null) {
      setTargetChoice(hostId);
    }
  }, [targetChoice, hostId, open]);

  const submit = async () => {
    const trimmed = name.trim();
    if (trimmed === "" || isPending || submitting.current) return;
    if (Array.from(trimmed).length > 100) {
      setSubmitError("Use a project name of at most 100 characters.");
      return;
    }
    if (projects.some((project) => project.name === trimmed)) {
      setSubmitError("A project with this name already exists. Use a different name.");
      return;
    }
    if (repositoryMode) {
      const error = repositorySelectionError(repository, repositoryCapabilities);
      if (error) {
        setSubmitError(error);
        return;
      }
      if (sandboxSelected && !providers.includes(sandboxProvider ?? null)) {
        setSubmitError("This sandbox target is unavailable. Choose another target.");
        return;
      }
      if (!sandboxSelected && repository.url.trim() !== "") {
        if (!selectedHost || selectedHost.status !== "online") {
          setSubmitError("Reconnect the selected host or choose another target.");
          return;
        }
        if (!selectedHost.clone_repository_supported) {
          setSubmitError("Update this host to clone repositories, or choose an existing folder.");
          return;
        }
        if (!isNavigablePath(destination) || destination.includes("\0")) {
          setSubmitError("Choose an absolute or home-relative path for the new checkout.");
          return;
        }
      }
    }
    const submission = ++submissionGeneration.current;
    setSubmitError(null);

    let selectedWorkspace = workspace;
    if (!repositoryMode && createWorkspaceFolder) {
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
          submitting.current = true;
          setPhase("creating");
          const path = await createDirectory.mutateAsync({
            hostId: dialogHostId,
            path: joinPath("~", trimmed),
          });
          if (submission !== submissionGeneration.current) return;
          selectedWorkspace = { hostId: dialogHostId, path };
          setCreatedWorkspace({ ...selectedWorkspace, projectName: trimmed });
        } catch (error) {
          if (submission !== submissionGeneration.current) return;
          submitting.current = false;
          setPhase(null);
          setSubmitError(friendlyDirectoryError(error));
          return;
        }
      }
    }

    if (repositoryMode) {
      selectedWorkspace = null;
      if (!sandboxSelected && repository.url.trim() !== "" && dialogHostId !== null) {
        const key = JSON.stringify([
          dialogHostId,
          repository.url.trim(),
          repository.branch.trim(),
          destination.trim(),
        ]);
        if (clonedWorkspace?.key === key) {
          selectedWorkspace = { hostId: dialogHostId, path: clonedWorkspace.path };
        } else {
          submitting.current = true;
          setPhase("cloning");
          try {
            const path = await cloneRepository.mutateAsync({
              hostId: dialogHostId,
              url: repository.url.trim(),
              ...(repository.branch.trim() ? { branch: repository.branch.trim() } : {}),
              path: destination.trim(),
            });
            if (submission !== submissionGeneration.current) return;
            setClonedWorkspace({ key, path });
            selectedWorkspace = { hostId: dialogHostId, path };
          } catch (error) {
            if (submission !== submissionGeneration.current) return;
            submitting.current = false;
            setPhase(null);
            const uncertain =
              !(error instanceof ApiError) ||
              error.code === "clone_transport_error" ||
              (error.status === 504 && error.code !== "clone_timeout");
            setSubmitError(
              uncertain
                ? `The clone result is unknown. Check ${destination} on the host before retrying. If the checkout is ready, select it in Folder mode.`
                : error.message,
            );
            return;
          }
        }
      }
    }
    if (submission !== submissionGeneration.current) return;
    submitting.current = true;
    setPhase("creating");
    const config: ProjectConfig = {};
    if (repositoryMode && repository.url.trim()) {
      config.repository = {
        url: repository.url.trim(),
        ...(repository.branch.trim() ? { branch: repository.branch.trim() } : {}),
      };
    }
    if (sandboxSelected) {
      config.host_id = SANDBOX_HOST_CHOICE;
      if (sandboxProvider) config.sandbox_provider = sandboxProvider;
    }
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
          setTargetChoice(null);
          setPhase(null);
          submitting.current = false;
          setWorkspace(null);
          setCreatedWorkspace(null);
          setWorkspacePickerOpen(false);
          setCreateWorkspaceFolder(false);
          setSubmitError(null);
          onCreated(project.name);
        },
        onError: (error) => {
          if (submission !== submissionGeneration.current) return;
          submitting.current = false;
          setPhase(null);
          setSubmitError(friendlyProjectError(error));
        },
      },
    );
  };

  const dialog = (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (submitting.current || isPending) return;
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
        aria-busy={isPending}
        showCloseButton={!isPending}
        onEscapeKeyDown={(event) => {
          if (submitting.current || isPending) event.preventDefault();
        }}
        onInteractOutside={(event) => {
          if (submitting.current || isPending) event.preventDefault();
        }}
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
        <fieldset disabled={isPending} className="contents">
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
                {repositoryCreation ? "Workspace" : "Working directory"}{" "}
                <span className="font-normal text-muted-foreground">(optional)</span>
              </span>
              <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground transition-transform duration-200 ease-out group-data-[state=open]:rotate-90" />
            </CollapsibleTrigger>
            <CollapsibleContent className="new-project-working-directory-content mt-2 flex flex-col gap-1.5">
              {repositoryCreation && (
                <>
                  <label htmlFor="new-project-target" className="text-sm font-medium">
                    Run on
                  </label>
                  <select
                    id="new-project-target"
                    value={targetChoice ?? ""}
                    onChange={(event) => {
                      targetEdited.current = true;
                      setTargetChoice(event.target.value || null);
                      setClonedWorkspace(null);
                      setWorkspace(null);
                      setCreatedWorkspace(null);
                      setSubmitError(null);
                    }}
                    className="rounded-md border border-input bg-background px-3 py-2 text-sm"
                  >
                    <option value="">Choose a target</option>
                    {targetChoice !== null &&
                      !sandboxSelected &&
                      !hosts.some((host) => host.host_id === targetChoice) && (
                        <option value={targetChoice}>{targetChoice} (unavailable)</option>
                      )}
                    {sandboxSelected && !providers.includes(sandboxProvider ?? null) && (
                      <option value={targetChoice!}>
                        {sandboxOptionLabel(sandboxProvider ?? null)} (unavailable)
                      </option>
                    )}
                    {providers.map((provider) => (
                      <option key={provider ?? "sandbox"} value={sandboxHostChoice(provider)}>
                        {sandboxOptionLabel(provider)}
                      </option>
                    ))}
                    {hosts
                      .filter((host) => !host.sandbox_provider)
                      .map((host) => (
                        <option key={host.host_id} value={host.host_id}>
                          {host.name}
                          {host.status !== "online" ? " (offline)" : ""}
                        </option>
                      ))}
                  </select>
                  {!sandboxSelected && (
                    <div className="flex gap-3 text-sm">
                      {["folder", "repository"].map((mode) => (
                        <label key={mode} className="flex items-center gap-1">
                          <input
                            type="radio"
                            name="workspace-mode"
                            value={mode}
                            checked={workspaceMode === mode}
                            onChange={() => {
                              setWorkspaceMode(mode);
                              setSubmitError(null);
                            }}
                          />
                          {mode === "folder" ? "Folder" : "Repository"}
                        </label>
                      ))}
                    </div>
                  )}
                </>
              )}
              {repositoryMode ? (
                <>
                  <SandboxRepositoryPicker
                    single
                    selections={repositories}
                    onChange={(next) => {
                      setRepositories(next);
                      setClonedWorkspace(null);
                    }}
                    maxRepos={1}
                    enabled={open && !isPending}
                    {...repositoryCapabilities}
                    testIdPrefix="new-project-repository"
                  />
                  {sandboxSelected ? (
                    <p className="text-xs text-muted-foreground">
                      Saved with this project. The repository will be prepared when you start a
                      session.
                    </p>
                  ) : (
                    <>
                      <label htmlFor="new-project-clone-path" className="text-sm font-medium">
                        Clone into
                      </label>
                      <div className="flex gap-2">
                        <input
                          id="new-project-clone-path"
                          value={destination}
                          onChange={(event) => {
                            setCloneDestination(event.target.value);
                            setClonedWorkspace(null);
                          }}
                          className="min-w-0 flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm"
                        />
                        <Button
                          type="button"
                          variant="outline"
                          onClick={() => setWorkspacePickerOpen(true)}
                          disabled={dialogHostId === null}
                        >
                          Browse
                        </Button>
                      </div>
                      <p className="text-xs text-muted-foreground">
                        The repository will be cloned before the project is created.
                      </p>
                      {selectedHost && !selectedHost.clone_repository_supported && (
                        <p className="text-xs text-destructive">
                          Update this host to clone repositories, or choose an existing folder.
                        </p>
                      )}
                    </>
                  )}
                </>
              ) : (
                <>
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
                          : workspace?.path ||
                            (hostsLoading ? "Loading hosts…" : "Choose a folder…")}
                      </span>
                    </span>
                    <span className="shrink-0 text-muted-foreground text-sm">Browse</span>
                  </button>
                </>
              )}
              <WorkspacePickerDialog
                open={workspacePickerOpen}
                onOpenChange={setWorkspacePickerOpen}
                hostId={dialogHostId}
                initialPath={repositoryMode ? undefined : workspace?.path}
                title={repositoryMode ? "Choose parent folder" : "Select working directory"}
                onConfirm={(path) => {
                  if (repositoryMode) {
                    setCloneDestination(joinPath(path, repositoryName));
                    setClonedWorkspace(null);
                  } else if (dialogHostId !== null) {
                    setWorkspace({ hostId: dialogHostId, path });
                  }
                }}
                description="Choose a default working directory for sessions in this project."
              />
            </CollapsibleContent>
          </Collapsible>
        </fieldset>
        {phase !== null && (
          <p role="status" aria-live="polite" className="text-sm">
            {phase === "cloning" ? "Cloning…" : "Creating project…"}
          </p>
        )}
        {(submitError || createProject.isError) && (
          <p className="text-ui text-destructive" role="alert">
            {submitError ?? friendlyProjectError(createProject.error)}
          </p>
        )}
        <DialogFooter className="border-t-0 bg-transparent">
          <Button
            type="button"
            variant="ghost"
            onClick={() => {
              if (!submitting.current) {
                submissionGeneration.current += 1;
                setOpen(false);
              }
            }}
            disabled={isPending}
          >
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
              if (submitting.current) return;
              submissionGeneration.current += 1;
              setName("");
              setIcon(undefined);
              targetEdited.current = false;
              setTargetChoice(initialTarget());
              setWorkspaceMode("folder");
              setRepositories([]);
              setCloneDestination(null);
              setClonedWorkspace(null);
              setPhase(null);
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
