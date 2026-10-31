import { useEffect, useRef, useState } from "react";
import { ChevronRightIcon, FolderOpenIcon, PlusIcon } from "lucide-react";
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
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { ProjectAppearanceButton } from "@/components/ProjectIconPicker";
import { useCreateProject, useProjects } from "@/hooks/useConversations";
import { useCloneHostRepository, useCreateHostDirectory } from "@/hooks/useHostFilesystem";
import type { Host } from "@/hooks/useHosts";
import { isFeatureEnabled, sandboxOptionLabel, sandboxProviderOptions } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  SANDBOX_HOST_CHOICE,
  sandboxHostChoice,
  sandboxHostChoiceProvider,
} from "@/lib/hostPreferences";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import type { ProjectAppearance } from "@/lib/projectColors";
import type { ProjectConfig } from "@/lib/projectsApi";
import type { LastSandboxRepo } from "@/lib/repoPreferences";
import { deriveRepoName, sandboxRepoProblem } from "@/lib/sandboxRepositories";
import { SandboxRepositoryPicker } from "./SandboxRepositoryPicker";
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

const UNMARKED: ProjectAppearance = { icon: null, color: null };

/**
 * "New project" control in the Projects group header. Opens a dialog that
 * creates an EMPTY first-class project (`POST /v1/projects`) — the capability
 * the legacy label model can't express. On success the new folder is expanded
 * (via `onCreated`) so the user can immediately file sessions into it.
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
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  // With nothing picked, the server assigns the least-used color on create.
  const [appearance, setAppearance] = useState<ProjectAppearance>(UNMARKED);
  // Where the project runs: a host id, or a sandbox choice (see sandboxHostChoice).
  const [runOn, setRunOn] = useState<string | null>(null);
  // A host the user picked, as opposed to the default: only a pick is saved for a name-only project.
  const runOnPicked = useRef(false);
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
  const hostName = hosts.find((host) => host.host_id === hostTarget)?.name ?? "the host";
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

  const submit = async () => {
    const trimmed = name.trim();
    if (trimmed === "" || isPending || repositorySubmitting) return;
    const submission = ++submissionGeneration.current;
    setSubmitError(null);

    // Label-only legacy projects (no id) can be promoted to a project of the same name.
    if (projects.some((project) => project.id !== null && project.name === trimmed)) {
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

    const config: ProjectConfig = {};
    if (appearance.icon) config.icon = appearance.icon;
    else if (appearance.color) config.color = appearance.color;
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
      } else if (runOnPicked.current || repositoryMode) {
        config.host_id = hostTarget;
      }
    }
    if (locking) config.repository = branch ? { url, branch } : { url };

    createProject.mutate(
      { name: trimmed, config: Object.keys(config).length > 0 ? config : undefined },
      {
        onSuccess: (project) => {
          if (submission !== submissionGeneration.current) return;
          submissionGeneration.current += 1;
          setOpen(false);
          setName("");
          setAppearance(UNMARKED);
          setRunOn(null);
          runOnPicked.current = false;
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
        },
        onError: (error) => {
          if (submission !== submissionGeneration.current) return;
          setRepositorySubmitting(false);
          if (checkoutPath !== null) {
            setSubmitError(
              `${friendlyProjectError(error)} The repository is already checked out at ${checkoutPath}.`,
            );
          }
        },
      },
    );
  };

  const dialog = (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // A clone or save in flight keeps the dialog open until it settles.
        if (!next && repositorySubmitting) return;
        if (!next) {
          submissionGeneration.current += 1;
        }
        setOpen(next);
        if (!next) {
          setWorkspacePickerOpen(false);
          setParentPickerOpen(false);
        }
      }}
    >
      <DialogContent className="sm:max-w-lg" onClick={(e) => e.stopPropagation()}>
        <DialogHeader>
          <DialogTitle>New project</DialogTitle>
          <DialogDescription>
            Create a project, then file sessions into it from a session's menu.
          </DialogDescription>
        </DialogHeader>
        <fieldset disabled={repositorySubmitting} className="contents">
          {/* One combined control: the mark tile (left) + name (right) share a
            single border, so it reads as a single input. The tile opens the
            color / emoji picker. */}
          <div className="flex items-stretch overflow-hidden rounded-lg border border-input">
            <ProjectAppearanceButton
              value={appearance}
              onChange={setAppearance}
              testId="new-project-icon"
            />
            <input
              autoFocus
              className="w-full bg-transparent px-3 py-2 text-ui outline-none"
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
          {repositoryCreation && (
            <div className="flex flex-col gap-1.5">
              <label htmlFor="new-project-run-on" className="font-medium text-ui">
                Run on
              </label>
              <select
                id="new-project-run-on"
                className="h-10 rounded-lg border border-input bg-transparent px-3 text-ui outline-none"
                value={runOn ?? ""}
                onChange={(event) => {
                  const next = event.target.value;
                  runOnPicked.current = true;
                  if (next !== runOn) {
                    // A submission still creating a folder for the old target must not save it.
                    submissionGeneration.current += 1;
                    setWorkspace(null);
                    setCreatedWorkspace(null);
                    setCreateWorkspaceFolder(false);
                    setCloneParent("~");
                    setDestinationOverride(null);
                  }
                  setRunOn(next);
                  setSubmitError(null);
                }}
              >
                {runOn === null && (
                  <option value="" disabled>
                    {hostsLoading ? "Loading hosts…" : "No host available"}
                  </option>
                )}
                {runOn !== null &&
                  !onlineHosts.some((host) => host.host_id === runOn) &&
                  !sandboxProviders.some((provider) => sandboxHostChoice(provider) === runOn) && (
                    <option value={runOn}>{hostsLoading ? "Loading hosts…" : hostName}</option>
                  )}
                {onlineHosts.map((host) => (
                  <option key={host.host_id} value={host.host_id}>
                    {host.name}
                  </option>
                ))}
                {sandboxProviders.map((provider) => (
                  <option key={sandboxHostChoice(provider)} value={sandboxHostChoice(provider)}>
                    {sandboxOptionLabel(provider)}
                  </option>
                ))}
              </select>
            </div>
          )}
          {repositoryCreation && !runsOnSandbox && hostTarget !== null && (
            <div role="radiogroup" aria-label="Project source" className="flex gap-4 text-ui">
              <label className="flex cursor-pointer items-center gap-2">
                <input
                  type="radio"
                  name="new-project-source"
                  value="folder"
                  checked={sourceMode === "folder"}
                  onChange={() => {
                    submissionGeneration.current += 1;
                    setSourceMode("folder");
                    setSubmitError(null);
                  }}
                />
                Folder
              </label>
              <label className="flex cursor-pointer items-center gap-2">
                <input
                  type="radio"
                  name="new-project-source"
                  value="repository"
                  checked={sourceMode === "repository"}
                  onChange={() => {
                    submissionGeneration.current += 1;
                    setSourceMode("repository");
                    setSubmitError(null);
                  }}
                />
                Repository
              </label>
            </div>
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
              />
              {repositories.length === 0 && (
                <input
                  aria-label="Branch"
                  placeholder="Default branch"
                  value={pendingRepoBranch}
                  onChange={(event) => {
                    setPendingRepoBranch(event.target.value);
                    setSubmitError(null);
                  }}
                  className="h-10 rounded-lg border border-input bg-transparent px-3 text-ui outline-none"
                />
              )}
              {hostTarget !== null ? (
                <>
                  <label htmlFor="new-project-clone-into" className="font-medium text-ui">
                    Clone into
                  </label>
                  <div className="flex items-center gap-2">
                    <input
                      id="new-project-clone-into"
                      className="h-10 min-w-0 flex-1 rounded-lg border border-input bg-transparent px-3 text-ui outline-none"
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
                      disabled={isPending}
                      onClick={() => setParentPickerOpen(true)}
                    >
                      Browse
                    </Button>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    Cloned onto {hostName} before the project is saved.
                  </p>
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
              ) : (
                <p className="text-xs text-muted-foreground">
                  Saved with this project. The repository will be prepared when you start a session.
                </p>
              )}
            </div>
          )}
          {folderMode && (
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
                  disabled={createWorkspaceFolder || hostTarget === null || isPending}
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
            onClick={() => setOpen(false)}
            disabled={isPending || repositorySubmitting}
          >
            Cancel
          </Button>
          <Button
            type="button"
            data-testid="new-project-confirm"
            loading={isPending}
            disabled={name.trim() === "" || isPending || repositorySubmitting}
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
              setAppearance(UNMARKED);
              setRunOn(hostId);
              runOnPicked.current = false;
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
