import { useEffect, useId, useMemo, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { MonitorCloudIcon, WorkflowIcon } from "lucide-react";

import {
  SandboxProfilePicker,
  useSandboxProfileCatalog,
  type SandboxProfileSelection,
} from "@/components/SandboxProfilePicker";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useHosts } from "@/hooks/useHosts";
import { readRecentWorkspaces, useRecentWorkspaces } from "@/hooks/useRecentWorkspaces";
import { markSandboxLaunch } from "@/hooks/useSandboxLaunchSelectionCheck";
import { previewIsUnsupported, useSandboxModelOptions } from "@/hooks/useSandboxModelOptions";
import type { SavedWorkflow } from "@/hooks/useSavedWorkflows";
import { partitionPickerEntries, selectableSessionAgents } from "@/lib/agentGrouping";
import { readLastAgentId } from "@/lib/agentPreferences";
import { pickerCuration, sandboxOptionLabel, sandboxProviderOptions } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { nativeModelLabel } from "@/lib/composerModelLabel";
import { getCliServerUrl } from "@/lib/host";
import {
  SANDBOX_HOST_CHOICE,
  readLastHostChoice,
  readLastSandboxProvider,
  sandboxHostChoice,
  sandboxHostChoiceProvider,
} from "@/lib/hostPreferences";
import { getCurrentUserId } from "@/lib/identity";
import {
  nativeCodingAgentForAvailableAgent,
  nativeWrapperLabelsForAgent,
} from "@/lib/nativeCodingAgents";
import { CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY } from "@/lib/nativeHarnessModes";
import { randomUUID } from "@/lib/randomUUID";
import {
  readLastSandboxRepos,
  writeLastSandboxRepos,
  type LastSandboxRepo,
} from "@/lib/repoPreferences";
import { useNavigate } from "@/lib/routing";
import { readLastSandboxProfile } from "@/lib/sandboxProfilePreferences";
import { SANDBOX_PROFILES_QUERY_KEY } from "@/lib/sandboxProfilesApi";
import {
  MAX_SANDBOX_REPOS,
  composeSandboxWorkspaces,
  isValidSandboxBranch,
  isValidSandboxWorkspaceLength,
} from "@/lib/sandboxRepositories";
import { createSession } from "@/lib/sessionsApi";
import { REVIEW_PLAN_FIRST, createWorkflowPrompt, runSavedWorkflowPrompt } from "@/lib/workflow";
import { ForkRunConfig, HostLabel, type ForkRunConfigValue } from "@/shell/ForkSessionDialog";
import { ConnectHostInstructions } from "@/shell/NewChatDialog";
import { SandboxRepositoryPicker } from "@/shell/SandboxRepositoryPicker";
import { resolveWorkspacePath, useResolvedHostHome } from "@/shell/WorkspacePicker";
import { WorkspacePathField } from "@/shell/WorkspacePathField";
import { WorkspacePickerDialog } from "@/shell/WorkspacePickerDialog";
import { setPendingInitialPrompt } from "@/store/chatStore";

/** What the dialog starts a session for: running a saved workflow, or drafting a new one. */
export type WorkflowSessionRequest =
  { kind: "run"; workflow: SavedWorkflow } | { kind: "create"; name: string; description: string };

export interface WorkflowSessionDialogProps {
  /** Keep it set while the dialog closes so the content doesn't blank. */
  request: WorkflowSessionRequest | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * Start a session for a workflow, to run a saved one ("Run workflow") or have
 * the agent draft a new one ("New workflow"): pick where it runs (a connected
 * host and directory, or a new sandbox and its repositories) and its agent,
 * check its first message, and start it. Laid out like the Clone session
 * dialog, whose host label and model, effort and permission fields it reuses.
 */
export function WorkflowSessionDialog({ request, open, onOpenChange }: WorkflowSessionDialogProps) {
  const name = request?.kind === "run" ? request.workflow.name : request?.name;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        data-testid="workflow-session-dialog"
        className="flex max-h-[85vh] flex-col gap-4 sm:max-w-lg"
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <WorkflowIcon className="size-4" />
            <span>{request?.kind === "create" ? "New workflow" : "Run workflow"}</span>
          </DialogTitle>
          <DialogDescription>
            {request?.kind === "create"
              ? `Starts a session where the agent drafts “${name}” with you.`
              : `Starts a session that runs “${name}”.`}
          </DialogDescription>
        </DialogHeader>
        {request && (
          <WorkflowSessionForm
            key={request.kind === "run" ? request.workflow.id : `create:${request.name}`}
            request={request}
            onClose={() => onOpenChange(false)}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

function Field({
  label,
  htmlFor,
  children,
}: {
  label: string;
  htmlFor?: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={htmlFor} className="text-sm font-medium text-muted-foreground">
        {label}
      </label>
      {children}
    </div>
  );
}

// An explicit "Default" pick means the agent's own default, which a new session gets by omission.
function pinnedValue(value: string | undefined): string | undefined {
  return value && value !== "default" ? value : undefined;
}

function WorkflowSessionForm({
  request,
  onClose,
}: {
  request: WorkflowSessionRequest;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const info = useServerInfo();
  const hostFieldId = useId();
  const directoryFieldId = useId();
  const agentFieldId = useId();
  const instructionsId = useId();
  const reviewId = useId();
  // Analytics ids, per flow.
  const flow = request.kind === "run" ? "run" : "new";

  const { data: hosts } = useHosts();
  const onlineHosts = useMemo(() => (hosts ?? []).filter((h) => h.status === "online"), [hosts]);
  const offlineHosts = useMemo(() => (hosts ?? []).filter((h) => h.status !== "online"), [hosts]);
  // One row per sandbox provider the server can launch, as on the new-session page.
  const sandboxRows = useMemo(
    () =>
      info !== "loading" && info.managed_sandboxes_enabled ? sandboxProviderOptions(info) : [],
    [info],
  );
  // A host id, or a sandbox row's sandboxHostChoice value.
  const [target, setTarget] = useState<string | null>(null);
  const sandboxRow = target === null ? undefined : sandboxHostChoiceProvider(target);
  const sandboxSelected = sandboxRow !== undefined;
  const hostId = target !== null && !sandboxSelected ? target : null;
  // The provider the sandbox launches on: its row's, else the server's first.
  const sandboxProvider = sandboxSelected
    ? (sandboxRow ?? (info !== "loading" ? info.sandbox_provider : null))
    : null;

  const [workspace, setWorkspace] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const { recent, addRecent } = useRecentWorkspaces(hostId);
  const home = useResolvedHostHome(hostId);
  const workspacePath = resolveWorkspacePath(workspace, home);
  const typedWorkspace = workspace.trim();
  // A "~" path waits for the host's home directory instead of showing an error.
  const workspaceInvalid =
    typedWorkspace !== "" && workspacePath === null && !typedWorkspace.startsWith("~");
  const hostOnline = onlineHosts.some((h) => h.host_id === hostId);

  // A host starts in its most recent directory.
  const selectTarget = (value: string) => {
    setTarget(value);
    setBrowsing(false);
    if (sandboxHostChoiceProvider(value) === undefined) {
      setWorkspace(readRecentWorkspaces(value)[0] ?? "");
    }
  };
  // Start where the new-session page last ran, a sandbox or an online host, when
  // that's still offered; else on a sandbox, as that page does, or the first online host.
  useEffect(() => {
    if (target !== null || hosts === undefined || info === "loading") return;
    const lastChoice = readLastHostChoice();
    const lastProvider = readLastSandboxProvider();
    const sandbox =
      sandboxRows.length > 0
        ? sandboxHostChoice(
            sandboxRows.includes(lastProvider) ? lastProvider : (sandboxRows[0] ?? null),
          )
        : null;
    const next =
      (lastChoice === SANDBOX_HOST_CHOICE ? sandbox : null) ??
      onlineHosts.find((h) => h.host_id === lastChoice)?.host_id ??
      sandbox ??
      onlineHosts[0]?.host_id ??
      null;
    if (next === null) return;
    setTarget(next);
    if (sandboxHostChoiceProvider(next) === undefined) {
      setWorkspace(readRecentWorkspaces(next)[0] ?? "");
    }
  }, [target, hosts, info, onlineHosts, sandboxRows]);

  // Repositories the sandbox clones, starting on the new-session page's last ones.
  const [sandboxRepos, setSandboxRepos] = useState<LastSandboxRepo[]>(readLastSandboxRepos);
  const [pendingRepoUrl, setPendingRepoUrl] = useState("");
  const capabilities =
    info !== "loading" && sandboxProvider !== null
      ? info.sandbox_provider_capabilities?.[sandboxProvider]
      : undefined;
  const maxRepos = capabilities?.multi_repo === true ? MAX_SANDBOX_REPOS : 1;
  // The picker explains each of these beside the repository.
  const reposInvalid =
    sandboxRepos.length > maxRepos ||
    sandboxRepos.some(
      (repo) =>
        !isValidSandboxBranch(repo.branch) || !isValidSandboxWorkspaceLength(repo.url, repo.branch),
    );
  const repoUrlPending = pendingRepoUrl.trim() !== "";
  const [sandboxProfile, setSandboxProfile] = useState<SandboxProfileSelection | null>(null);
  const rememberedProfileId = useMemo(
    () => readLastSandboxProfile(sandboxProvider),
    [sandboxProvider],
  );
  const { launchError: profileError, selection: profileSelection } = useSandboxProfileCatalog(
    sandboxProvider,
    sandboxProfile,
    rememberedProfileId,
  );

  const { data: agents } = useAvailableAgents();
  const curation = pickerCuration(info);
  const agentList = useMemo(
    () => selectableSessionAgents(agents ?? [], curation.restrictToAgentNames),
    [agents, curation.restrictToAgentNames],
  );
  const { harnesses, agents: composedAgents } = useMemo(
    () => partitionPickerEntries(agentList),
    [agentList],
  );
  const [pickedAgentId, setPickedAgentId] = useState<string | null>(null);
  const [lastAgentId] = useState(readLastAgentId);
  // The user's pick, else the agent the new-session page last used, else the default.
  const agent =
    agentList.find((a) => a.id === pickedAgentId) ??
    agentList.find((a) => a.id === lastAgentId) ??
    agentList.find((a) => a.name === curation.defaultAgentName) ??
    agentList[0];
  const [runConfig, setRunConfig] = useState<ForkRunConfigValue>({});

  // A sandbox's models come from its provider; a host lists its own.
  const harness = agent
    ? (nativeCodingAgentForAvailableAgent(agent)?.harness ?? agent.harness ?? null)
    : null;
  const sandboxModelsOffered =
    sandboxSelected &&
    (capabilities?.inference_models === true || capabilities?.default_models === true);
  const sandboxModels = useSandboxModelOptions(
    sandboxProvider,
    harness,
    agent?.id ?? null,
    getCurrentUserId(),
    sandboxModelsOffered,
  );
  const agentUnsupported =
    sandboxModelsOffered && previewIsUnsupported(sandboxModels)
      ? (sandboxModels.data?.error ?? `${agent?.display_name} can’t run in this sandbox.`)
      : null;

  const [instructions, setInstructions] = useState(() =>
    request.kind === "run"
      ? runSavedWorkflowPrompt(request.workflow)
      : createWorkflowPrompt(request.name, request.description),
  );
  // A saved workflow runs without a plan review unless asked.
  const [reviewPlanFirst, setReviewPlanFirst] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const targetReady = sandboxSelected
    ? profileError === null && !reposInvalid && !repoUrlPending && agentUnsupported === null
    : hostOnline && workspacePath !== null;
  const canRun = targetReady && agent !== undefined && instructions.trim() !== "" && !submitting;

  async function run() {
    if (!canRun || agent === undefined) return;
    setSubmitting(true);
    setError(null);
    const text =
      request.kind === "run" && reviewPlanFirst
        ? `${instructions.trim()}\n\n${REVIEW_PLAN_FIRST}`
        : instructions.trim();
    // A sandbox create can carry the first message, which the server sends once the sandbox is up.
    const serverSendsText =
      sandboxSelected && info !== "loading" && info.durable_initial_input === true;
    const modelOverride = pinnedValue(runConfig.modelOverride);
    const reasoningEffort = pinnedValue(runConfig.reasoningEffort);
    try {
      const session = await createSession(agent.id, [], {
        // The drafting session is named for the workflow it creates.
        title: request.kind === "create" ? `Create workflow: ${request.name}` : undefined,
        ...(sandboxSelected
          ? {
              sandbox: {
                provider: sandboxProvider,
                profileId: profileSelection?.id,
                profileRevision: profileSelection?.revision,
                workspaces: composeSandboxWorkspaces(sandboxRepos),
                // Some servers have the client name the new sandbox's host.
                hostId:
                  info !== "loading" && info.request_managed_sandbox_host_id
                    ? randomUUID().replace(/-/g, "")
                    : undefined,
              },
              initialInput: serverSendsText
                ? {
                    type: "message",
                    data: { role: "user", content: [{ type: "input_text", text }] },
                  }
                : undefined,
            }
          : { hostId: hostId ?? undefined, workspace: workspacePath ?? undefined }),
        labels: {
          ...nativeWrapperLabelsForAgent(agent),
          ...(runConfig.codexBypassSandbox ? { [CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY]: "1" } : {}),
        },
        terminalLaunchArgs: runConfig.terminalLaunchArgs?.length
          ? runConfig.terminalLaunchArgs
          : undefined,
        modelOverride,
        reasoningEffort,
      });
      // Otherwise the session page sends it as the first message once the session is ready.
      if (!serverSendsText) setPendingInitialPrompt(session.id, { text, skill: null, files: [] });
      if (sandboxSelected) {
        writeLastSandboxRepos(sandboxRepos);
        // The session page drops a picked model or effort the new sandbox doesn't offer.
        const launchedModel = sandboxModels.data?.models.find((m) => m.id === modelOverride);
        markSandboxLaunch(session.id, {
          model: modelOverride ?? null,
          effort: reasoningEffort ?? null,
          modelLabel: launchedModel ? nativeModelLabel(launchedModel) : null,
        });
      } else if (workspacePath !== null) {
        addRecent(workspacePath);
      }
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      onClose();
      navigate(`/c/${session.id}`);
    } catch (e) {
      // A profile can change under the dialog; reload them so the next try sees it.
      if (sandboxSelected)
        void queryClient.invalidateQueries({ queryKey: SANDBOX_PROFILES_QUERY_KEY });
      setError(e instanceof Error ? e.message : "Couldn’t start the session. Try again.");
      setSubmitting(false);
    }
  }

  return (
    <>
      {/* The room keeps the scroll area from clipping the fields' focus rings. */}
      <div className="-mx-1 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-1">
        <Field label="Host" htmlFor={hostFieldId}>
          {hosts === undefined ? (
            <p className="text-sm text-muted-foreground">Loading hosts…</p>
          ) : onlineHosts.length === 0 && sandboxRows.length === 0 ? (
            <ConnectHostInstructions
              serverUrl={getCliServerUrl()}
              label={
                hosts.length === 0
                  ? "No hosts connected yet. Connect one from your terminal:"
                  : "No hosts online. Reconnect one from your terminal:"
              }
              onSshConnected={selectTarget}
            />
          ) : (
            <Select
              value={target ?? ""}
              onValueChange={selectTarget}
              componentId={`workflows.${flow}.host`}
            >
              <SelectTrigger
                id={hostFieldId}
                className="w-full text-sm"
                data-testid="workflow-session-host"
              >
                <SelectValue
                  placeholder={
                    sandboxRows.length > 0 ? "Select a host or sandbox" : "Select a host"
                  }
                />
              </SelectTrigger>
              <SelectContent>
                {sandboxRows.map((provider) => (
                  <SelectItem key={provider ?? "default"} value={sandboxHostChoice(provider)}>
                    <span className="flex items-center gap-2">
                      <MonitorCloudIcon className="size-4 text-muted-foreground" />
                      <span className="text-sm">{sandboxOptionLabel(provider)}</span>
                    </span>
                  </SelectItem>
                ))}
                {sandboxRows.length > 0 && hosts.length > 0 && <SelectSeparator />}
                {[...onlineHosts, ...offlineHosts].map((host) => (
                  <SelectItem
                    key={host.host_id}
                    value={host.host_id}
                    disabled={host.status !== "online"}
                  >
                    <HostLabel host={host} />
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          {sandboxProvider !== null && (
            <SandboxProfilePicker
              provider={sandboxProvider}
              value={sandboxProfile}
              onChange={setSandboxProfile}
              rememberedProfileId={rememberedProfileId}
            />
          )}
          {sandboxSelected && profileError !== null && (
            <p role="status" className="text-sm text-muted-foreground">
              {profileError}
            </p>
          )}
        </Field>

        {hostId !== null && hostOnline && (
          <Field label="Working directory" htmlFor={directoryFieldId}>
            <WorkspacePathField
              hostId={hostId}
              value={workspace}
              onChange={setWorkspace}
              onBrowse={() => setBrowsing((value) => !value)}
              onCommit={(path) => {
                setWorkspace(path);
                setBrowsing(true);
              }}
              recent={recent}
              dropdownDisabled={browsing}
              inputId={directoryFieldId}
              placeholder="Type a path or browse…"
              invalid={workspaceInvalid}
            />
            {workspaceInvalid && (
              <p className="text-sm text-destructive">
                Enter an absolute path, like /home/you/project or ~/project.
              </p>
            )}
            <WorkspacePickerDialog
              open={browsing}
              onOpenChange={setBrowsing}
              hostId={hostId}
              initialPath={workspacePath ?? ""}
              onConfirm={setWorkspace}
            />
          </Field>
        )}

        {sandboxSelected && (
          <Field label={maxRepos > 1 ? "Repositories" : "Repository"}>
            <SandboxRepositoryPicker
              selections={sandboxRepos}
              onChange={(repos) => setSandboxRepos(repos)}
              maxRepos={maxRepos}
              testIdPrefix="workflow-session-sandbox-repo"
              pendingUrl={pendingRepoUrl}
              onPendingUrlChange={setPendingRepoUrl}
            />
            {sandboxRepos.length === 0 && (
              <p className="text-sm text-muted-foreground">
                With no repository, the session starts in an empty workspace.
              </p>
            )}
            {repoUrlPending && (
              <p className="text-sm text-muted-foreground">
                Add or clear the repository URL to run.
              </p>
            )}
          </Field>
        )}

        <Field label="Agent" htmlFor={agentFieldId}>
          <Select
            value={agent?.id ?? ""}
            onValueChange={setPickedAgentId}
            componentId={`workflows.${flow}.agent`}
          >
            <SelectTrigger
              id={agentFieldId}
              className="w-full text-sm"
              data-testid="workflow-session-agent"
            >
              <SelectValue
                placeholder={agents === undefined ? "Loading agents…" : "Select an agent"}
              />
            </SelectTrigger>
            <SelectContent position="popper" align="start">
              {harnesses.map((entry) => (
                <SelectItem key={entry.id} value={entry.id} className="text-sm">
                  {entry.display_name}
                </SelectItem>
              ))}
              {harnesses.length > 0 && composedAgents.length > 0 && <SelectSeparator />}
              {composedAgents.map((entry) => (
                <SelectItem key={entry.id} value={entry.id} className="text-sm">
                  {entry.display_name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {agentUnsupported !== null && (
            <p className="text-sm text-destructive">{agentUnsupported}</p>
          )}
        </Field>

        {/* Model, effort and permission settings for a native agent; remounts per agent. */}
        {agent !== undefined && (
          <ForkRunConfig
            key={agent.id}
            targetHarness={nativeCodingAgentForAvailableAgent(agent)?.harness ?? null}
            targetAgent={agent}
            sourceSession={null}
            sameFamilyAsSource={false}
            sameAgentAsSource={false}
            selectedHostId={hostOnline ? hostId : null}
            modelCatalog={
              sandboxSelected
                ? { options: sandboxModels.data?.models ?? [], loading: sandboxModels.isLoading }
                : undefined
            }
            onChange={setRunConfig}
          />
        )}

        <Field label="Instructions" htmlFor={instructionsId}>
          <Textarea
            id={instructionsId}
            value={instructions}
            onChange={(event) => setInstructions(event.currentTarget.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) void run();
            }}
            rows={4}
            className="min-h-24 resize-y text-sm focus-visible:ring-0"
            data-testid="workflow-session-instructions"
            componentId={`workflows.${flow}.instructions`}
          />
        </Field>

        {request.kind === "run" && (
          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <Checkbox
                id={reviewId}
                checked={reviewPlanFirst}
                onCheckedChange={(checked) => setReviewPlanFirst(checked === true)}
                data-testid="workflow-session-review-plan"
                componentId="workflows.run.review_plan_first"
              />
              <label htmlFor={reviewId} className="cursor-pointer text-ui">
                Review the plan before work starts
              </label>
            </div>
            <p className="pl-6 text-sm text-muted-foreground">
              After exploring, the agent shows you its plan and waits for your approval.
            </p>
          </div>
        )}
      </div>

      {error !== null && (
        <p role="alert" className="text-sm text-destructive" data-testid="workflow-session-error">
          {error}
        </p>
      )}

      <DialogFooter>
        <Button
          variant="ghost"
          onClick={onClose}
          disabled={submitting}
          componentId={`workflows.${flow}.cancel`}
        >
          Cancel
        </Button>
        <Button
          onClick={() => void run()}
          disabled={!canRun}
          loading={submitting}
          data-testid="workflow-session-submit"
          componentId={`workflows.${flow}.submit`}
        >
          {request.kind === "run" ? "Run" : "Create"}
        </Button>
      </DialogFooter>
    </>
  );
}
