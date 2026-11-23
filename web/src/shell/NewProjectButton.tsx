import { type CSSProperties, useEffect, useRef, useState } from "react";
import {
  ChevronRightIcon,
  FolderOpenIcon,
  MonitorCloudIcon,
  PlusIcon,
  SmilePlusIcon,
} from "lucide-react";
import {
  ComposerHostTrigger,
  ComposerWorkspaceTrigger,
} from "@/components/composer/ComposerControls";
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
import { Input } from "@/components/ui/input";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { EmojiPicker } from "@/components/ProjectIconPicker";
import { useCreateProject, useProjects } from "@/hooks/useConversations";
import { useCloneHostRepository, useCreateHostDirectory } from "@/hooks/useHostFilesystem";
import type { Host } from "@/hooks/useHosts";
import { useRecentWorkspaces } from "@/hooks/useRecentWorkspaces";
import { isFeatureEnabled, sandboxOptionLabel, sandboxProviderOptions } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  SANDBOX_HOST_CHOICE,
  sandboxHostChoice,
  sandboxHostChoiceProvider,
} from "@/lib/hostPreferences";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import type { ProjectConfig } from "@/lib/projectsApi";
import { useNavigate } from "@/lib/routing";
import { projectOrchestratorPath } from "@/projectHome/projectHomeNavigation";
import { ApiError } from "@/lib/sessionsApi";
import { reuseCreationAttempt, type CreationAttempt } from "@/lib/creationAttempt";
import { shouldGuardDialogDismiss } from "@/lib/dialogDismissGuard";
import type { LastSandboxRepo } from "@/lib/repoPreferences";
import { deriveRepoName, sandboxRepoProblem } from "@/lib/sandboxRepositories";
import { cn } from "@/lib/utils";
import { SandboxRepositoryPicker } from "./SandboxRepositoryPicker";
import { joinPath } from "./WorkspacePicker";
import { WorkspacePickerDialog } from "./WorkspacePickerDialog";
import { HostOption } from "./NewChatDialog";
import { RecentWorkspaceList } from "./RecentWorkspaceList";
import {
  ProjectOrchestratorPicker,
  useProjectOrchestratorChoices,
} from "./ProjectOrchestratorPicker";

const PROJECT_CONTROL_CLASS = "h-10 rounded-lg px-3 text-ui mobile-app:h-11";

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
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
    return error.message;
  }
  return "The project couldn’t be created. Try again.";
}

function friendlyCloneError(error: unknown): string {
  const message = error instanceof Error ? error.message.toLowerCase() : "";
  if (message.includes("update this host")) {
    return "This host is too old to clone repositories. Update omnigent on it and try again.";
  }
  if (message.includes("already exists")) {
    return "That destination already exists. It may be a checkout from an earlier attempt: create a folder project from it, or choose a different folder name.";
  }
  if (message.includes("lost contact")) {
    return "The host stopped responding during the clone. The clone may have finished, so check the destination before retrying.";
  }
  if (message.includes("did not finish")) {
    return "The host didn’t finish the clone in time. It may still finish, so check the destination before retrying.";
  }
  if (message.includes("took too long")) {
    return "The clone took too long and was stopped. Delete the partial folder, then try again.";
  }
  if (message.includes("doesn't exist or isn't writable")) {
    return "The parent folder doesn’t exist or can’t be written to. Choose another location.";
  }
  if (message.includes("could not clone")) {
    return "Git couldn’t clone the repository. Check the URL, the branch, and the host’s Git access.";
  }
  if (
    message.includes("host not found") ||
    message.includes("host is offline") ||
    message.includes("disconnected")
  ) {
    return "The host is offline. Reconnect it and try again.";
  }
  return "The repository couldn’t be cloned. Check the details and try again.";
}

/**
 * "New project" control in the Projects group header. Opens a dialog that
 * creates a project and its Orchestrator session (`POST /v1/projects`).
 *
 * When the server supports project repositories, the dialog also takes a
 * "Run on" target: a connected host (a folder, or a repository cloned onto it
 * before the project is saved) or a sandbox provider (the repository is saved
 * and cloned when a session starts).
 */
export function NewProjectButton({
  onCreated,
  hostId,
  hosts,
  hostsLoading,
}: {
  onCreated: (name: string) => void;
  hostId: string | null;
  hosts: Host[];
  hostsLoading: boolean;
}) {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [icon, setIcon] = useState<string | undefined>(undefined);
  const [emojiOpen, setEmojiOpen] = useState(false);
  // Where the project runs: a host id, or a sandbox choice (see sandboxHostChoice).
  const [runOn, setRunOn] = useState<string | null>(null);
  const [hostMenuOpen, setHostMenuOpen] = useState(false);
  const [workspaceMenuOpen, setWorkspaceMenuOpen] = useState(false);
  const creationAttempt = useRef<CreationAttempt | undefined>(undefined);
  const dropdownOpen = useRef(false);
  const dropdownClosedAt = useRef(0);
  const onDropdownOpenChange = (nextOpen: boolean) => {
    dropdownOpen.current = nextOpen;
    if (!nextOpen) dropdownClosedAt.current = Date.now();
  };
  const guardDialogDismiss = (event: {
    target: EventTarget | null;
    preventDefault: () => void;
  }) => {
    if (
      shouldGuardDialogDismiss(event.target, {
        selectOpen: dropdownOpen.current,
        msSinceSelectClose: Date.now() - dropdownClosedAt.current,
      })
    )
      event.preventDefault();
  };
  const [sourceMode, setSourceMode] = useState<"folder" | "repository">("folder");
  const [workspace, setWorkspace] = useState<WorkspaceSelection | null>(null);
  const [createdWorkspace, setCreatedWorkspace] = useState<
    | (WorkspaceSelection & {
        projectName: string;
      })
    | null
  >(null);
  const [workspacePickerOpen, setWorkspacePickerOpen] = useState(false);
  const [createWorkspaceFolder, setCreateWorkspaceFolder] = useState(false);
  const [repositories, setRepositories] = useState<LastSandboxRepo[]>([]);
  const [pendingRepoUrl, setPendingRepoUrl] = useState("");
  const [pendingRepoBranch, setPendingRepoBranch] = useState("");
  const [cloneParent, setCloneParent] = useState("~");
  // Set once the user types a destination; until then it follows the repository name.
  const [destinationOverride, setDestinationOverride] = useState<string | null>(null);
  const [parentPickerOpen, setParentPickerOpen] = useState(false);
  // A checkout that succeeded; reused when only the name changes after a failed save.
  const [checkout, setCheckout] = useState<{ key: string; path: string } | null>(null);
  const [repositorySubmitting, setRepositorySubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const submissionGeneration = useRef(0);
  // Safari confirms an IME candidate with an Enter that isn't flagged as
  // composing, so track composition like the chat composer does.
  const isComposingRef = useRef(false);
  const serverInfo = useServerInfo();
  const repositoryCreation =
    serverInfo !== "loading" && isFeatureEnabled(serverInfo, "project_repository_creation");
  const sandboxProviders =
    repositoryCreation && serverInfo.managed_sandboxes_enabled === true
      ? sandboxProviderOptions(serverInfo)
      : [];
  const { data: projects = [] } = useProjects();
  const createProject = useCreateProject();
  const createDirectory = useCreateHostDirectory();
  const cloneRepository = useCloneHostRepository();
  const isPending =
    createProject.isPending || createDirectory.isPending || cloneRepository.isPending;

  const onlineHosts = hosts.filter((host) => host.status === "online" && !host.sandbox_provider);
  const sandboxProvider = runOn === null ? undefined : sandboxHostChoiceProvider(runOn);
  const runsOnSandbox = sandboxProvider !== undefined;
  const hostTarget = runOn !== null && !runsOnSandbox ? runOn : null;
  const selectedHost = hosts.find((host) => host.host_id === hostTarget);
  const hostName = selectedHost?.name ?? "Choose host";
  const orchestrator = useProjectOrchestratorChoices(
    open,
    selectedHost,
    runsOnSandbox,
    sandboxProvider ?? null,
  );
  const { recent, addRecent } = useRecentWorkspaces(hostTarget);
  // Folder UI applies to host targets (and the legacy folder-only dialog).
  const folderMode = !runsOnSandbox && (!repositoryCreation || sourceMode === "folder");
  const repositoryMode = repositoryCreation && (runsOnSandbox || sourceMode === "repository");
  const selectedRepository = repositories[0];
  const repoUrl = repositoryMode ? (selectedRepository?.url ?? pendingRepoUrl).trim() : "";
  const repoBranch = repositoryMode
    ? (selectedRepository ? selectedRepository.branch : pendingRepoBranch).trim()
    : "";
  const repoName = repoUrl === "" ? null : deriveRepoName(repoUrl);
  const destinationDefault = repoName ? joinPath(cloneParent, repoName) : "";
  const destinationShown = destinationOverride ?? destinationDefault;
  const folderCreationDisabled = isPending || (!hostsLoading && hostTarget === null);

  useEffect(() => {
    if (open && runOn === null && hostId !== null) {
      setRunOn(hostId);
    }
  }, [runOn, hostId, open]);

  const selectHost = (next: string) => {
    if (next !== runOn) {
      submissionGeneration.current += 1;
      setWorkspace(null);
      setCreatedWorkspace(null);
      setCreateWorkspaceFolder(false);
      setCloneParent("~");
      setDestinationOverride(null);
    }
    setRunOn(next);
    setSubmitError(null);
  };

  const submit = async () => {
    const trimmed = name.trim();
    if (trimmed === "" || isPending || repositorySubmitting) return;
    const submission = ++submissionGeneration.current;
    setSubmitError(null);
    if (!orchestrator.selected || orchestrator.loading) {
      setSubmitError("No native agent is available.");
      return;
    }
    if (runOn === null) {
      setSubmitError("Choose a host.");
      return;
    }
    if (!runsOnSandbox && selectedHost?.status !== "online") {
      setSubmitError("The host is offline. Reconnect it and try again.");
      return;
    }
    if (orchestrator.unavailable || orchestrator.options.unsupported) {
      setSubmitError("This harness is unavailable on the selected host.");
      return;
    }

    // Label-only legacy projects (no id) can be promoted to a project of the same name.
    if (
      !creationAttempt.current &&
      projects.some((project) => project.id !== null && project.name === trimmed)
    ) {
      setSubmitError("A project with this name already exists. Use a different name.");
      return;
    }
    const url = repoUrl;
    const branch = repoBranch;
    const destination = (destinationOverride ?? destinationDefault).trim();
    const locking = repositoryMode && url !== "";
    if (repositoryMode) {
      // Also catches a branch typed without a URL.
      const problem = sandboxRepoProblem(url, branch);
      if (problem) {
        setSubmitError(problem.message);
        return;
      }
    }
    if (locking) {
      if (runOn === null) {
        setSubmitError("Choose where to run the repository.");
        return;
      }
      if (hostTarget !== null) {
        if (destination === "") {
          setSubmitError("Choose where to clone the repository.");
          return;
        }
        if (!onlineHosts.some((host) => host.host_id === hostTarget)) {
          setSubmitError("The host is offline. Reconnect it and try again.");
          return;
        }
      }
    }

    let selectedWorkspace = folderMode ? workspace : null;
    if (folderMode && createWorkspaceFolder) {
      if (!isSinglePathComponent(trimmed)) {
        setSubmitError("Use a project name without slashes or dot segments to create a folder.");
        return;
      }
      if (hostTarget === null) {
        setSubmitError("Connect a host before creating a working directory.");
        return;
      }
      if (createdWorkspace?.hostId === hostTarget && createdWorkspace.projectName === trimmed) {
        selectedWorkspace = createdWorkspace;
      } else {
        try {
          const path = await createDirectory.mutateAsync({
            hostId: hostTarget,
            path: joinPath("~", trimmed),
          });
          if (submission !== submissionGeneration.current) return;
          selectedWorkspace = { hostId: hostTarget, path };
          setCreatedWorkspace({ ...selectedWorkspace, projectName: trimmed });
        } catch (error) {
          if (submission !== submissionGeneration.current) return;
          setSubmitError(friendlyDirectoryError(error));
          return;
        }
      }
    }

    if (submission !== submissionGeneration.current) return;
    if (locking) setRepositorySubmitting(true);
    let checkoutPath: string | null = null;
    if (locking && hostTarget !== null) {
      const key = [hostTarget, url, branch, destination].join("\n");
      if (checkout?.key === key) {
        checkoutPath = checkout.path;
      } else {
        try {
          checkoutPath = await cloneRepository.mutateAsync({
            hostId: hostTarget,
            url,
            branch: branch || undefined,
            path: destination,
          });
        } catch (error) {
          if (submission !== submissionGeneration.current) return;
          setRepositorySubmitting(false);
          setSubmitError(friendlyCloneError(error));
          return;
        }
        if (submission !== submissionGeneration.current) return;
        setCheckout({ key, path: checkoutPath });
      }
    }

    const config: ProjectConfig = { ...orchestrator.config };
    if (icon) config.icon = icon;
    if (runsOnSandbox) {
      config.host_id = SANDBOX_HOST_CHOICE;
      if (sandboxProvider) config.sandbox_provider = sandboxProvider;
    } else if (hostTarget !== null) {
      if (checkoutPath !== null) {
        config.host_id = hostTarget;
        config.workspace = checkoutPath;
      } else if (selectedWorkspace) {
        config.host_id = selectedWorkspace.hostId;
        config.workspace = selectedWorkspace.path;
      } else {
        config.host_id = hostTarget;
      }
    }
    if (locking) config.repository = branch ? { url, branch } : { url };
    creationAttempt.current = reuseCreationAttempt(creationAttempt.current, {
      name: trimmed,
      config,
    });

    createProject.mutate(
      { name: trimmed, config, idempotencyKey: creationAttempt.current.key },
      {
        onSuccess: (project) => {
          if (submission !== submissionGeneration.current) return;
          submissionGeneration.current += 1;
          setOpen(false);
          setName("");
          setIcon(undefined);
          setRunOn(null);
          creationAttempt.current = undefined;
          if (config.workspace) addRecent(config.workspace);
          setSourceMode("folder");
          setWorkspace(null);
          setCreatedWorkspace(null);
          setWorkspacePickerOpen(false);
          setCreateWorkspaceFolder(false);
          setRepositories([]);
          setPendingRepoUrl("");
          setPendingRepoBranch("");
          setDestinationOverride(null);
          setParentPickerOpen(false);
          setCheckout(null);
          setRepositorySubmitting(false);
          setSubmitError(null);
          onCreated(project.name);
          if (
            serverInfo !== "loading" &&
            serverInfo.project_tickets === true &&
            isFeatureEnabled(serverInfo, "project_kanban")
          ) {
            navigate(projectOrchestratorPath(project.name));
          }
        },
        onError: (error) => {
          if (submission !== submissionGeneration.current) return;
          setRepositorySubmitting(false);
          setSubmitError(
            checkoutPath !== null
              ? `${friendlyProjectError(error)} The repository is already checked out at ${checkoutPath}.`
              : friendlyProjectError(error),
          );
        },
      },
    );
  };

  const dialog = (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // A clone or save in flight keeps the dialog open until it settles.
        if (!next && (repositorySubmitting || isPending)) return;
        if (!next) {
          submissionGeneration.current += 1;
        }
        setOpen(next);
        if (!next) {
          setEmojiOpen(false);
          setWorkspacePickerOpen(false);
          setParentPickerOpen(false);
        }
      }}
    >
      <DialogContent
        className="max-h-[85dvh] overflow-y-auto sm:max-w-lg"
        onPointerDownOutside={guardDialogDismiss}
        onInteractOutside={guardDialogDismiss}
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
          <DialogDescription className="sr-only">Create a project.</DialogDescription>
        </DialogHeader>
        <fieldset disabled={repositorySubmitting || isPending} className="contents">
          {/* One combined control: emoji tile (left) + name (right) share a
            single border, so it reads as a single input. */}
          <div className="flex h-10 items-stretch overflow-hidden rounded-lg border border-input focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50 mobile-app:h-11">
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
            <Input
              autoFocus
              className="h-full rounded-none border-0 px-3 focus-visible:ring-0"
              placeholder="Project name…"
              value={name}
              onChange={(e) => setName(e.target.value)}
              onCompositionStart={() => {
                isComposingRef.current = true;
              }}
              onCompositionEnd={() => {
                isComposingRef.current = false;
              }}
              onKeyDown={(e) => {
                if (isImeCompositionKeyEvent(e, isComposingRef.current)) return;
                if (e.key === "Enter") {
                  e.preventDefault();
                  void submit();
                }
              }}
            />
          </div>
          <section aria-label="Run on" className="flex flex-col gap-3 border-t pt-4">
            <h3 className="text-xs font-medium text-muted-foreground">Run on</h3>
            <DropdownMenu
              open={hostMenuOpen}
              onOpenChange={(nextOpen) => {
                setHostMenuOpen(nextOpen);
                onDropdownOpenChange(nextOpen);
              }}
            >
              <DropdownMenuTrigger asChild>
                <ComposerHostTrigger
                  aria-label="Run on"
                  data-testid="new-project-run-on"
                  showLabel
                  label={
                    runsOnSandbox
                      ? sandboxOptionLabel(sandboxProvider)
                      : hostsLoading
                        ? "Loading hosts…"
                        : hostName
                  }
                  status={runsOnSandbox ? undefined : (selectedHost?.status ?? "unknown")}
                  cloud={runsOnSandbox || !!selectedHost?.sandbox_provider}
                  className={cn(
                    PROJECT_CONTROL_CLASS,
                    "w-full justify-start gap-2 border border-input text-foreground outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 md:h-10",
                  )}
                />
              </DropdownMenuTrigger>
              <DropdownMenuContent
                align="start"
                className="composer-host-menu w-(--radix-dropdown-menu-trigger-width)"
              >
                {(sandboxProviders.length > 0 || hosts.some((host) => host.sandbox_provider)) && (
                  <>
                    <div className="px-2 py-1 text-xs text-muted-foreground">Cloud</div>
                    {sandboxProviders.map((provider) => (
                      <DropdownMenuItem
                        key={provider}
                        onSelect={() => selectHost(sandboxHostChoice(provider))}
                      >
                        <MonitorCloudIcon className="size-3.5 text-muted-foreground" />
                        {sandboxOptionLabel(provider)}
                      </DropdownMenuItem>
                    ))}
                    {hosts
                      .filter((host) => host.sandbox_provider)
                      .map((host) => (
                        <DropdownMenuItem
                          key={host.host_id}
                          disabled={host.status !== "online"}
                          onSelect={() => selectHost(host.host_id)}
                        >
                          <HostOption host={host} cloud />
                        </DropdownMenuItem>
                      ))}
                    <DropdownMenuSeparator />
                  </>
                )}
                <div className="px-2 py-1 text-xs text-muted-foreground">My machines</div>
                {hosts
                  .filter((host) => !host.sandbox_provider)
                  .map((host) => (
                    <DropdownMenuItem
                      key={host.host_id}
                      disabled={host.status !== "online"}
                      onSelect={() => selectHost(host.host_id)}
                    >
                      <HostOption host={host} />
                    </DropdownMenuItem>
                  ))}
                {!hosts.length && (
                  <div className="px-2 py-1.5 text-sm text-muted-foreground">
                    No hosts connected yet.
                  </div>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
            {repositoryCreation && !runsOnSandbox && hostTarget !== null && (
              <Tabs
                value={sourceMode}
                onValueChange={(value) => {
                  if (value !== "folder" && value !== "repository") return;
                  submissionGeneration.current += 1;
                  setSourceMode(value);
                  setSubmitError(null);
                }}
              >
                <TabsList
                  aria-label="Project source"
                  className="w-full group-data-horizontal/tabs:h-10 mobile-app:min-h-11"
                >
                  <TabsTrigger value="folder" disabled={repositorySubmitting || isPending}>
                    Folder
                  </TabsTrigger>
                  <TabsTrigger value="repository" disabled={repositorySubmitting || isPending}>
                    Repository
                  </TabsTrigger>
                </TabsList>
              </Tabs>
            )}
            {repositoryMode && (
              <div className="flex flex-col gap-2" data-testid="new-project-repository">
                <SandboxRepositoryPicker
                  selections={repositories}
                  onChange={(next) => {
                    // The picker adds a URL with a blank branch; keep the branch typed for it.
                    const typedBranch = pendingRepoBranch.trim();
                    if (repositories.length === 0) {
                      setRepositories(
                        next.map((repo) =>
                          repo.branch === "" && typedBranch !== ""
                            ? { ...repo, branch: typedBranch }
                            : repo,
                        ),
                      );
                      setPendingRepoBranch("");
                    } else {
                      setRepositories(next);
                    }
                    setSubmitError(null);
                  }}
                  maxRepos={1}
                  pendingUrl={pendingRepoUrl}
                  onPendingUrlChange={(url) => {
                    setPendingRepoUrl(url);
                    setSubmitError(null);
                  }}
                  testIdPrefix="new-project-repo"
                  controlClassName={PROJECT_CONTROL_CLASS}
                  addButtonClassName="w-20 justify-center"
                />
                {repositories.length === 0 && (
                  <Input
                    aria-label="Branch"
                    placeholder="Default branch"
                    value={pendingRepoBranch}
                    onChange={(event) => {
                      setPendingRepoBranch(event.target.value);
                      setSubmitError(null);
                    }}
                    className={PROJECT_CONTROL_CLASS}
                  />
                )}
                {hostTarget !== null ? (
                  <>
                    <label htmlFor="new-project-clone-into" className="text-sm">
                      Clone into
                    </label>
                    <div className="flex items-center gap-2">
                      <Input
                        id="new-project-clone-into"
                        className={cn(PROJECT_CONTROL_CLASS, "flex-1")}
                        placeholder="~/repository"
                        value={destinationShown}
                        onChange={(event) => {
                          setDestinationOverride(event.target.value);
                          setSubmitError(null);
                        }}
                      />
                      <Button
                        type="button"
                        variant="outline"
                        className={cn(PROJECT_CONTROL_CLASS, "w-20 border-input")}
                        disabled={isPending}
                        onClick={() => setParentPickerOpen(true)}
                      >
                        Browse
                      </Button>
                    </div>
                    <WorkspacePickerDialog
                      open={parentPickerOpen}
                      onOpenChange={setParentPickerOpen}
                      hostId={hostTarget}
                      initialPath={cloneParent}
                      title="Choose parent folder"
                      description="Choose the folder the repository will be cloned into."
                      onConfirm={(path) => {
                        setCloneParent(path);
                        setDestinationOverride(null);
                      }}
                    />
                  </>
                ) : null}
              </div>
            )}
            {folderMode && (
              <Collapsible>
                <CollapsibleTrigger
                  className={cn(
                    PROJECT_CONTROL_CLASS,
                    "group flex w-full cursor-pointer items-center gap-2 border border-input text-left outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50",
                  )}
                >
                  <span>
                    Working directory{" "}
                    <span className="font-normal text-muted-foreground">(optional)</span>
                  </span>
                  <ChevronRightIcon className="ml-auto size-4 shrink-0 text-muted-foreground transition-transform duration-200 ease-out group-data-[state=open]:rotate-90" />
                </CollapsibleTrigger>
                <CollapsibleContent className="new-project-working-directory-content mt-2 flex flex-col gap-2 [&>span]:w-full [&>span]:max-w-none">
                  <label
                    htmlFor="new-project-create-workspace"
                    className={`flex min-h-10 items-center gap-2 rounded-lg border border-input bg-transparent px-3 py-1.5 text-ui mobile-app:min-h-11 ${
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
                  <Popover
                    modal
                    open={workspaceMenuOpen}
                    onOpenChange={(nextOpen) => {
                      setWorkspaceMenuOpen(nextOpen);
                      onDropdownOpenChange(nextOpen);
                    }}
                  >
                    <PopoverTrigger asChild>
                      <ComposerWorkspaceTrigger
                        id="new-project-workspace"
                        data-testid="new-project-workspace"
                        kind="directory"
                        menuOpen={workspaceMenuOpen}
                        tooltip={workspace?.path || "Working directory"}
                        label={
                          createWorkspaceFolder
                            ? joinPath("~", name.trim() || "Project name")
                            : workspace?.path || "Choose a folder…"
                        }
                        aria-label={`Working directory: ${createWorkspaceFolder ? joinPath("~", name.trim() || "Project name") : workspace?.path || "Not selected"}`}
                        disabled={createWorkspaceFolder || hostTarget === null || isPending}
                        className={cn(
                          PROJECT_CONTROL_CLASS,
                          "w-full justify-start gap-2 border-input outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 [&>svg:last-child]:ml-auto",
                        )}
                      />
                    </PopoverTrigger>
                    <PopoverContent align="start" className="w-80 max-w-[calc(100vw-2rem)] p-1.5">
                      {recent.length > 0 && (
                        <>
                          <div className="px-2 py-1 text-xs text-muted-foreground">Recents</div>
                          <RecentWorkspaceList
                            hostId={hostTarget}
                            paths={recent}
                            selectedPath={workspace?.path}
                            onSelect={(path) => {
                              if (hostTarget) setWorkspace({ hostId: hostTarget, path });
                              setWorkspaceMenuOpen(false);
                              onDropdownOpenChange(false);
                            }}
                            onBrowse={(path) => {
                              if (hostTarget) setWorkspace({ hostId: hostTarget, path });
                              setWorkspaceMenuOpen(false);
                              onDropdownOpenChange(false);
                              setWorkspacePickerOpen(true);
                            }}
                          />
                        </>
                      )}
                      <Button
                        variant="ghost"
                        size="sm"
                        className="w-full justify-start"
                        onClick={() => {
                          setWorkspaceMenuOpen(false);
                          onDropdownOpenChange(false);
                          setWorkspacePickerOpen(true);
                        }}
                      >
                        <FolderOpenIcon />
                        Open folder…
                      </Button>
                    </PopoverContent>
                  </Popover>
                  <WorkspacePickerDialog
                    open={workspacePickerOpen}
                    onOpenChange={setWorkspacePickerOpen}
                    hostId={hostTarget}
                    initialPath={workspace?.path}
                    onConfirm={(path) => {
                      if (hostTarget !== null) {
                        setWorkspace({ hostId: hostTarget, path });
                      }
                    }}
                    description="Choose a default working directory for sessions in this project."
                  />
                </CollapsibleContent>
              </Collapsible>
            )}
          </section>
          <section aria-label="Orchestrator" className="space-y-3 border-t pt-4">
            <h3 className="text-xs font-medium text-muted-foreground">Orchestrator</h3>
            <ProjectOrchestratorPicker
              choices={orchestrator}
              host={selectedHost}
              sandbox={runsOnSandbox}
              onOpenChange={onDropdownOpenChange}
            />
          </section>
          {(submitError || createProject.isError) && (
            <p className="text-ui text-destructive" role="alert">
              {submitError ?? friendlyProjectError(createProject.error)}
            </p>
          )}
        </fieldset>
        <DialogFooter className="border-t-0 bg-transparent">
          <Button
            type="button"
            variant="ghost"
            className={PROJECT_CONTROL_CLASS}
            onClick={() => setOpen(false)}
            disabled={isPending || repositorySubmitting}
          >
            Cancel
          </Button>
          <Button
            type="button"
            className={PROJECT_CONTROL_CLASS}
            data-testid="new-project-confirm"
            loading={isPending}
            disabled={
              name.trim() === "" ||
              isPending ||
              repositorySubmitting ||
              orchestrator.loading ||
              !orchestrator.selected
            }
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
              setRunOn(hostId);
              creationAttempt.current = undefined;
              setSourceMode("folder");
              setWorkspace(null);
              setCreatedWorkspace(null);
              setWorkspacePickerOpen(false);
              setCreateWorkspaceFolder(false);
              setRepositories([]);
              setPendingRepoUrl("");
              setPendingRepoBranch("");
              setCloneParent("~");
              setDestinationOverride(null);
              setParentPickerOpen(false);
              setCheckout(null);
              setRepositorySubmitting(false);
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
