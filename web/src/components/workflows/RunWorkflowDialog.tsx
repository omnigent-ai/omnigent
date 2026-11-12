import { useEffect, useId, useMemo, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { WorkflowIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
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
import type { SavedWorkflow } from "@/hooks/useSavedWorkflows";
import { partitionPickerEntries, selectableSessionAgents } from "@/lib/agentGrouping";
import { readLastAgentId } from "@/lib/agentPreferences";
import { pickerCuration } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { getCliServerUrl } from "@/lib/host";
import { readLastHostChoice } from "@/lib/hostPreferences";
import {
  nativeCodingAgentForAvailableAgent,
  nativeWrapperLabelsForAgent,
} from "@/lib/nativeCodingAgents";
import { CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY } from "@/lib/nativeHarnessModes";
import { useNavigate } from "@/lib/routing";
import { createSession } from "@/lib/sessionsApi";
import { runSavedWorkflowPrompt } from "@/lib/workflow";
import { ForkRunConfig, HostLabel, type ForkRunConfigValue } from "@/shell/ForkSessionDialog";
import { ConnectHostInstructions } from "@/shell/NewChatDialog";
import { resolveWorkspacePath, useResolvedHostHome } from "@/shell/WorkspacePicker";
import { WorkspacePathField } from "@/shell/WorkspacePathField";
import { WorkspacePickerDialog } from "@/shell/WorkspacePickerDialog";
import { setPendingInitialPrompt } from "@/store/chatStore";

export interface RunWorkflowDialogProps {
  /** The workflow to run. Keep it set while the dialog closes so the content doesn't blank. */
  workflow: SavedWorkflow | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * Run a saved workflow: pick the host, directory and agent for a new session,
 * check its first message, and start it. Laid out like the Clone session
 * dialog, whose host label and model, effort and permission fields it reuses.
 */
export function RunWorkflowDialog({ workflow, open, onOpenChange }: RunWorkflowDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        data-testid="run-workflow-dialog"
        className="flex max-h-[85vh] flex-col gap-4 sm:max-w-lg"
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <WorkflowIcon className="size-4" />
            <span>Run workflow</span>
          </DialogTitle>
          <DialogDescription>Starts a session that runs “{workflow?.name}”.</DialogDescription>
        </DialogHeader>
        {workflow && (
          <RunWorkflowForm
            key={workflow.id}
            workflow={workflow}
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

function RunWorkflowForm({ workflow, onClose }: { workflow: SavedWorkflow; onClose: () => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const info = useServerInfo();
  const hostFieldId = useId();
  const directoryFieldId = useId();
  const agentFieldId = useId();
  const instructionsId = useId();

  const { data: hosts } = useHosts();
  const onlineHosts = useMemo(() => (hosts ?? []).filter((h) => h.status === "online"), [hosts]);
  const offlineHosts = useMemo(() => (hosts ?? []).filter((h) => h.status !== "online"), [hosts]);
  const [hostId, setHostId] = useState<string | null>(null);
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

  // Each host starts in its most recent directory.
  const selectHost = (id: string) => {
    setHostId(id);
    setWorkspace(readRecentWorkspaces(id)[0] ?? "");
    setBrowsing(false);
  };
  // Start on the host the new-session page last used if it's online, else the first online one.
  useEffect(() => {
    if (hostId !== null || onlineHosts.length === 0) return;
    const lastHostId = readLastHostChoice();
    const host = onlineHosts.find((h) => h.host_id === lastHostId) ?? onlineHosts[0]!;
    setHostId(host.host_id);
    setWorkspace(readRecentWorkspaces(host.host_id)[0] ?? "");
  }, [hostId, onlineHosts]);

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

  const [instructions, setInstructions] = useState(() => runSavedWorkflowPrompt(workflow));
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const canRun =
    hostOnline &&
    workspacePath !== null &&
    agent !== undefined &&
    instructions.trim() !== "" &&
    !submitting;

  async function run() {
    if (!canRun || hostId === null || workspacePath === null || agent === undefined) return;
    setSubmitting(true);
    setError(null);
    try {
      const session = await createSession(agent.id, [], {
        hostId,
        workspace: workspacePath,
        labels: {
          ...nativeWrapperLabelsForAgent(agent),
          ...(runConfig.codexBypassSandbox ? { [CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY]: "1" } : {}),
        },
        terminalLaunchArgs: runConfig.terminalLaunchArgs?.length
          ? runConfig.terminalLaunchArgs
          : undefined,
        modelOverride: pinnedValue(runConfig.modelOverride),
        reasoningEffort: pinnedValue(runConfig.reasoningEffort),
      });
      // The session page sends this as the first message once the session is ready.
      setPendingInitialPrompt(session.id, { text: instructions.trim(), skill: null, files: [] });
      addRecent(workspacePath);
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      onClose();
      navigate(`/c/${session.id}`);
    } catch (e) {
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
          ) : onlineHosts.length === 0 ? (
            <ConnectHostInstructions
              serverUrl={getCliServerUrl()}
              label={
                hosts.length === 0
                  ? "No hosts connected yet. Connect one from your terminal:"
                  : "No hosts online. Reconnect one from your terminal:"
              }
              onSshConnected={selectHost}
            />
          ) : (
            <Select
              value={hostId ?? ""}
              onValueChange={selectHost}
              componentId="workflows.run.host"
            >
              <SelectTrigger
                id={hostFieldId}
                className="w-full text-sm"
                data-testid="run-workflow-host"
              >
                <SelectValue placeholder="Select a host" />
              </SelectTrigger>
              <SelectContent>
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

        <Field label="Agent" htmlFor={agentFieldId}>
          <Select
            value={agent?.id ?? ""}
            onValueChange={setPickedAgentId}
            componentId="workflows.run.agent"
          >
            <SelectTrigger
              id={agentFieldId}
              className="w-full text-sm"
              data-testid="run-workflow-agent"
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
            data-testid="run-workflow-instructions"
            componentId="workflows.run.instructions"
          />
        </Field>
      </div>

      {error !== null && (
        <p role="alert" className="text-sm text-destructive" data-testid="run-workflow-error">
          {error}
        </p>
      )}

      <DialogFooter>
        <Button
          variant="ghost"
          onClick={onClose}
          disabled={submitting}
          componentId="workflows.run.cancel"
        >
          Cancel
        </Button>
        <Button
          onClick={() => void run()}
          disabled={!canRun}
          loading={submitting}
          data-testid="run-workflow-submit"
          componentId="workflows.run.submit"
        >
          Run
        </Button>
      </DialogFooter>
    </>
  );
}
