import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { AlertTriangleIcon, GitBranchIcon, MonitorCloudIcon } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { DisabledActionTooltip } from "@/components/DisabledActionTooltip";
import { useSessionActionRestrictions } from "@/hooks/useSessionActionRestrictions";
import { SESSION_ACTIONS_LOADING } from "@/lib/sessionCapabilities";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { parentOf, resolveWorkspacePath } from "./WorkspacePicker";
import { WorkspacePickerDialog } from "./WorkspacePickerDialog";
import { WorkspacePathField } from "./WorkspacePathField";
import { HostLabel } from "./HostLabel";
import { normalizeWorkspacePath } from "./NewChatDialog";
import { fetchHosts, useHosts } from "@/hooks/useHosts";
import { useSession } from "@/hooks/useSession";
import { useHostFilesystem } from "@/hooks/useHostFilesystem";
import { useRecentWorkspaces } from "@/hooks/useRecentWorkspaces";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { sandboxOptionLabel, sandboxProviderOptions } from "@/lib/capabilities";
import { sandboxHostChoice, sandboxHostChoiceProvider } from "@/lib/hostPreferences";
import { managedSandboxHostId } from "@/lib/managedSandboxHost";
import { composeSandboxWorkspace, sandboxRepoProblem } from "@/lib/sandboxRepositories";
import {
  getSessionSlim,
  launchManagedHost,
  launchRunner,
  stopSession,
  updateSession,
} from "@/lib/sessionsApi";
import { terminalsQueryKey, type TerminalInfo } from "@/lib/terminals";
import { captureLaunchStatus, useChatStore } from "@/store/chatStore";

/**
 * Refresh the binding, stop unless its host is freshly confirmed offline, then release and launch.
 * Generic unbind remains nondestructive.
 */
export function SwitchHostDialog(props: Parameters<typeof SupportedSwitchHostDialog>[0]) {
  const { switchHostDisabledReason } = useSessionActionRestrictions(props.sessionId, {
    hostId: props.currentHostId,
  });
  if (switchHostDisabledReason) {
    const loading = switchHostDisabledReason === SESSION_ACTIONS_LOADING;
    return (
      <Dialog open={props.open} onOpenChange={props.onOpenChange}>
        <DialogContent data-testid="switch-host-dialog" className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Switch host</DialogTitle>
            <DialogDescription role="status">{switchHostDisabledReason}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => props.onOpenChange(false)}>
              Cancel
            </Button>
            {!loading && (
              <DisabledActionTooltip reason={switchHostDisabledReason} label="Switch host">
                <Button data-testid="switch-host-button" disabled>
                  Switch host
                </Button>
              </DisabledActionTooltip>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }
  return <SupportedSwitchHostDialog {...props} />;
}

function SupportedSwitchHostDialog({
  open,
  onOpenChange,
  sessionId,
  currentHostId,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  sessionId: string;
  currentHostId: string | null;
}) {
  const queryClient = useQueryClient();
  const { data: hosts } = useHosts({ enabled: open });
  const markRunnerLaunched = useChatStore((s) => s.markRunnerLaunched);

  const [selectedHostId, setSelectedHostId] = useState<string | null>(null);
  // A new server-provisioned sandbox picked instead of a connected host; its
  // provider is null on a server that names none.
  const [sandboxSelected, setSandboxSelected] = useState(false);
  const [sandboxProvider, setSandboxProvider] = useState<string | null>(null);
  // Repository cloned into the sandbox, recomposed into `<url>[#<branch>]`.
  const [sandboxRepoUrl, setSandboxRepoUrl] = useState("");
  const [sandboxRepoBranch, setSandboxRepoBranch] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [originAvailable, setOriginAvailable] = useState(false);
  const [moveState, setMoveState] = useState<"initial" | "stopped" | "released">("initial");
  const stranded = moveState !== "initial";

  // Sandbox rows appear only when the server can launch a sandbox and move a
  // session onto it; "loading" keeps them hidden until the boot probe resolves.
  const info = useServerInfo();
  const moveSupported =
    info !== "loading" && info.managed_sandboxes_enabled && info.move_to_managed_sandbox;
  // A half-finished switch can leave the session with no host, and the server
  // only moves a session off a host it still has.
  const { session } = useSession(sessionId);
  const sessionHostless = session != null && session.hostId === null;
  const sandboxProviderRows = useMemo(() => {
    if (info === "loading" || !moveSupported || sessionHostless) return [];
    // Only providers the server says take a moved session (not one that manages
    // sandbox profiles): picking another would release the runner for a move it
    // refuses.
    return sandboxProviderOptions(info).filter(
      (provider) =>
        info.sandbox_provider_capabilities?.[provider ?? info.sandbox_provider ?? ""]
          ?.accepts_session_move === true,
    );
  }, [info, moveSupported, sessionHostless]);

  const { recent, addRecent } = useRecentWorkspaces(selectedHostId);

  // Pinned at mount — the badge mounts this dialog only while it is open.
  // A successful switch repoints the session's host, so filtering on the
  // live prop would drop the just-picked host out of its own list mid-
  // submit and blank the select, which renders the chosen item's label.
  const [originHostId] = useState(currentHostId);

  // A completed stop or release makes the online origin a recovery target too.
  const targetHosts = useMemo(
    () =>
      (hosts ?? []).filter(
        (h) => h.status === "online" && (originAvailable || h.host_id !== originHostId),
      ),
    [hosts, originHostId, originAvailable],
  );

  // Land on a usable selection instead of an empty form: take the first
  // available host as soon as the list arrives, else the first sandbox.
  useEffect(() => {
    if (!open || sandboxSelected || selectedHostId !== null || hosts === undefined) return;
    if (targetHosts.length > 0) {
      setSelectedHostId(targetHosts[0].host_id);
    } else if (sandboxProviderRows.length > 0) {
      setSandboxSelected(true);
      setSandboxProvider(sandboxProviderRows[0]);
    }
  }, [open, sandboxSelected, selectedHostId, hosts, targetHosts, sandboxProviderRows]);

  // Drop a sandbox pick whose row is gone, e.g. once the session lost its host.
  useEffect(() => {
    if (sandboxSelected && !sandboxProviderRows.includes(sandboxProvider)) {
      setSandboxSelected(false);
      setSandboxProvider(null);
    }
  }, [sandboxSelected, sandboxProvider, sandboxProviderRows]);

  // Home listing for the chosen host. Shares the picker's query key, so
  // opening the browser afterwards costs no extra request.
  const { data: homeListing, isPlaceholderData } = useHostFilesystem(
    selectedHostId,
    selectedHostId === null ? null : "",
  );
  // Entries share one parent, so the home listing's first entry resolves the
  // host's absolute home directory.
  const firstHomeEntry = homeListing && !isPlaceholderData ? homeListing.entries[0] : undefined;
  const resolvedHome = firstHomeEntry ? parentOf(firstHomeEntry.path) : null;

  // A path is meaningless on a machine it didn't come from, so clear the
  // field whenever the target changes and let the default below refill it.
  const prevHostId = useRef<string | null>(null);
  const userEditedRef = useRef(false);
  useEffect(() => {
    if (prevHostId.current === selectedHostId) return;
    prevHostId.current = selectedHostId;
    setWorkspace("");
    userEditedRef.current = false;
  }, [selectedHostId]);

  // Default the directory for the chosen host: the last workspace used
  // there, else its home. Never overwrites typed text — a late-arriving
  // home listing must not clobber what the user is entering.
  useEffect(() => {
    if (!open || selectedHostId === null || workspace !== "" || userEditedRef.current) return;
    const preferred = recent[0] ?? resolvedHome;
    if (preferred) setWorkspace(preferred);
  }, [open, selectedHostId, workspace, recent, resolvedHome]);

  function handleWorkspaceChange(next: string): void {
    userEditedRef.current = true;
    setWorkspace(next);
  }

  function handleOpenChange(next: boolean): void {
    if (!next) {
      setSelectedHostId(null);
      setSandboxSelected(false);
      setSandboxProvider(null);
      setSandboxRepoUrl("");
      setSandboxRepoBranch("");
      setWorkspace("");
      setBrowsing(false);
      setError(null);
      setOriginAvailable(false);
      setMoveState("initial");
      setSubmitting(false);
      prevHostId.current = null;
      userEditedRef.current = false;
    }
    onOpenChange(next);
  }

  // Resolve a typed "~/…" path to its absolute form against the host's home
  // (resolvedHome, above), so it's directly submittable without opening the
  // tree browser (the server never expands ~). Already-absolute values pass
  // through; a tilde path stays unresolved until the home listing arrives.
  const resolvedWorkspace = resolveWorkspacePath(workspace, resolvedHome);
  const workspaceTrimmed = resolvedWorkspace ?? normalizeWorkspacePath(workspace) ?? "";
  const workspaceValid = resolvedWorkspace !== null;

  function commitWorkspacePath(path: string): void {
    handleWorkspaceChange(path);
    setBrowsing(true);
  }

  function selectHost(hostId: string): void {
    setSandboxSelected(false);
    setSelectedHostId(hostId);
  }

  function selectSandbox(provider: string | null): void {
    setSandboxSelected(true);
    setSandboxProvider(provider);
    // The repository fields replace the host directory, which has nothing
    // left to browse.
    setSelectedHostId(null);
    setBrowsing(false);
  }

  const repoProblem = sandboxRepoProblem(sandboxRepoUrl, sandboxRepoBranch);
  const repositoryError = repoProblem?.field === "repository" ? repoProblem.message : null;
  const branchError = repoProblem?.field === "branch" ? repoProblem.message : null;
  const canSwitch = sandboxSelected
    ? repoProblem === null
    : selectedHostId !== null && workspaceValid;

  async function handleSwitch(): Promise<void> {
    if (!canSwitch) return;
    setSubmitting(true);
    setError(null);
    setMoveState("initial");
    let stopped = false;
    let released = false;
    try {
      // A launch can land before its response or cache refresh. Resolve the
      // current binding and routing affinity before Stop or an offline exemption.
      let boundHostId: string | null;
      try {
        boundHostId = (await getSessionSlim(sessionId)).hostId ?? null;
      } catch (e) {
        const detail = e instanceof Error ? e.message : "Session lookup failed.";
        throw new Error(
          `Couldn't refresh the session's current host. Check your connection and try again. ${detail}`,
          { cause: e },
        );
      }
      const currentHost = hosts?.find((host) => host.host_id === boundHostId);
      let confirmedOffline = false;
      if (currentHost?.status === "offline") {
        try {
          confirmedOffline = (await fetchHosts(true)).some(
            (host) => host.host_id === boundHostId && host.status === "offline",
          );
        } catch (e) {
          const detail = e instanceof Error ? e.message : "Host lookup failed.";
          throw new Error(
            `Couldn't refresh the current host's status. Check your connection and try again. ${detail}`,
            { cause: e },
          );
        } finally {
          void queryClient.invalidateQueries({ queryKey: ["hosts"] });
        }
      }
      if (!confirmedOffline) {
        try {
          await stopSession(sessionId);
        } catch (e) {
          const detail = e instanceof Error ? e.message : "Couldn't stop the old session.";
          throw new Error(
            `${detail} Check the current host and stop the session there, then try again.`,
            { cause: e },
          );
        }
        stopped = true;
        setOriginAvailable(true);
        setMoveState("stopped");
      }
      // Drop the model override alongside the runner binding. A model id is
      // host-bound — the catalog depends on that machine's credentials — so
      // carrying it over lands the next turn on "model may not exist or you
      // may not have access to it". Cleared, the new runner resolves the
      // harness default, which is by definition available there. `silent`
      // keeps this off the transcript: it's part of the move, not a /model.
      await updateSession(sessionId, { runnerId: "", modelOverride: null, silent: true });
      // From here the session is unbound until the launch lands.
      released = true;
      setOriginAvailable(true);
      setMoveState("released");
      if (sandboxSelected) {
        // The launch's own events report progress from here, but its first ones
        // may go to another replica's stream; the response shows where it is.
        const showLaunch = captureLaunchStatus(sessionId);
        // Route the launch by the new sandbox's host id.
        const sandboxHostId = await managedSandboxHostId(info);
        const moved = await launchManagedHost(sessionId, {
          provider: sandboxProvider,
          workspace: composeSandboxWorkspace(sandboxRepoUrl, sandboxRepoBranch) ?? null,
          ...(sandboxHostId ? { hostId: sandboxHostId } : {}),
        });
        showLaunch(moved);
      } else if (selectedHostId !== null) {
        await launchRunner(selectedHostId, sessionId, workspaceTrimmed);
        // The new runner is booting but nothing on the wire says so yet, and
        // no turn is in flight to imply it — without this the session reads as
        // idle and the move lands on a silent, empty chat.
        markRunnerLaunched();
        addRecent(workspaceTrimmed);
      }
      // Close before refreshing. The session refetch reports the new host,
      // and keeping the dialog on screen for two round trips leaves it
      // sitting in "Switching…" long after the move has landed.
      handleOpenChange(false);
      void queryClient.invalidateQueries({ queryKey: ["session", sessionId] });
      void queryClient.invalidateQueries({ queryKey: ["session-agent", sessionId] });
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      // The old host's shells don't follow the session, but the terminals
      // cache is SSE-primary with union-on-fetch semantics and never goes
      // stale (staleTime: Infinity in useTerminals) — left alone, the strip
      // keeps rendering the previous host's terminals. Clear, then refetch
      // from the new runner; an entry racing the fetch survives via the
      // queryFn union.
      queryClient.setQueryData<TerminalInfo[]>(terminalsQueryKey(sessionId), []);
      void queryClient.invalidateQueries({ queryKey: terminalsQueryKey(sessionId) });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't switch hosts. Try again.");
      // Stop or launch may have changed the session before a later request failed.
      if (stopped || released)
        void queryClient.invalidateQueries({ queryKey: ["session", sessionId] });
    } finally {
      setSubmitting(false);
    }
  }

  const noTargets =
    hosts !== undefined && targetHosts.length === 0 && sandboxProviderRows.length === 0;

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent data-testid="switch-host-dialog" className="flex flex-col gap-4 sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Switch host</DialogTitle>
          <DialogDescription>
            {sandboxProviderRows.length > 0
              ? "Move this session to another connected machine or a new sandbox."
              : "Move this session to another connected machine."}
          </DialogDescription>
        </DialogHeader>

        {noTargets ? (
          <p className="text-xs text-muted-foreground" data-testid="switch-host-no-targets">
            No other hosts are online. Connect one with <code>omnigent host</code> and try again.
          </p>
        ) : (
          <>
            <div className="flex flex-col gap-2">
              <span className="text-xs font-medium text-muted-foreground">Host</span>
              <Select
                value={
                  sandboxSelected ? sandboxHostChoice(sandboxProvider) : (selectedHostId ?? "")
                }
                onValueChange={(v) => {
                  const provider = sandboxHostChoiceProvider(v);
                  if (provider !== undefined) selectSandbox(provider);
                  else selectHost(v);
                }}
                componentId="switch_host.host"
              >
                <SelectTrigger className="w-full text-xs" data-testid="switch-host-select">
                  <SelectValue placeholder="Select a host" />
                </SelectTrigger>
                <SelectContent>
                  {sandboxProviderRows.map((provider) => (
                    <SelectItem
                      key={provider ?? "default"}
                      value={sandboxHostChoice(provider)}
                      data-testid={`switch-host-sandbox-option-${provider ?? "default"}`}
                    >
                      <span className="flex items-center gap-2">
                        <MonitorCloudIcon className="size-4 text-muted-foreground" />
                        <span>{sandboxOptionLabel(provider)}</span>
                      </span>
                    </SelectItem>
                  ))}
                  {sandboxProviderRows.length > 0 && targetHosts.length > 0 && <SelectSeparator />}
                  {targetHosts.map((host) => (
                    <SelectItem
                      key={host.host_id}
                      value={host.host_id}
                      data-testid={`switch-host-option-${host.host_id}`}
                    >
                      <HostLabel host={host} />
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {sandboxSelected ? (
              <div className="flex flex-col gap-3">
                <div className="flex flex-col gap-1">
                  <label
                    htmlFor="switch-host-sandbox-repo"
                    className="text-xs font-medium text-muted-foreground"
                  >
                    Repository (optional)
                  </label>
                  <input
                    id="switch-host-sandbox-repo"
                    type="text"
                    value={sandboxRepoUrl}
                    onChange={(e) => setSandboxRepoUrl(e.target.value)}
                    placeholder="https://github.com/org/repo"
                    data-testid="switch-host-sandbox-repo-input"
                    aria-invalid={repositoryError !== null || undefined}
                    className="rounded-md border border-input aria-invalid:border-destructive bg-background px-3 py-2 font-mono text-xs outline-none transition-colors focus-visible:border-ring"
                  />
                  {repositoryError !== null ? (
                    <p
                      role="alert"
                      className="text-xs text-destructive"
                      data-testid="switch-host-sandbox-repo-error"
                    >
                      {repositoryError}
                    </p>
                  ) : (
                    <p className="text-xs text-muted-foreground">
                      Cloned into the sandbox as the session's working directory. Leave blank to
                      start in an empty sandbox.
                    </p>
                  )}
                </div>
                <div className="flex flex-col gap-1">
                  <label
                    htmlFor="switch-host-sandbox-branch"
                    className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground"
                  >
                    <GitBranchIcon className="size-3.5" />
                    Branch (optional)
                  </label>
                  <input
                    id="switch-host-sandbox-branch"
                    type="text"
                    value={sandboxRepoBranch}
                    onChange={(e) => setSandboxRepoBranch(e.target.value)}
                    placeholder="main"
                    data-testid="switch-host-sandbox-branch-input"
                    aria-invalid={branchError !== null || undefined}
                    className="rounded-md border border-input aria-invalid:border-destructive bg-background px-3 py-2 font-mono text-xs outline-none transition-colors focus-visible:border-ring"
                  />
                  {branchError !== null && (
                    <p
                      role="alert"
                      className="text-xs text-destructive"
                      data-testid="switch-host-sandbox-branch-error"
                    >
                      {branchError}
                    </p>
                  )}
                </div>
                <p
                  className="flex items-start gap-1.5 text-xs text-warning"
                  data-testid="switch-host-warning"
                >
                  <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
                  <span>
                    Files don't move into the sandbox. Anything uncommitted stays on the old
                    machine, and the agent picks up in the new sandbox instead.
                  </span>
                </p>
              </div>
            ) : (
              <div className="flex flex-col gap-2">
                <span className="text-xs font-medium text-muted-foreground">Working directory</span>
                {selectedHostId ? (
                  <>
                    <WorkspacePathField
                      hostId={selectedHostId}
                      value={workspace}
                      onChange={handleWorkspaceChange}
                      onBrowse={() => setBrowsing((v) => !v)}
                      onCommit={commitWorkspacePath}
                      recent={recent}
                      dropdownDisabled={browsing}
                    />
                    <WorkspacePickerDialog
                      open={browsing}
                      onOpenChange={setBrowsing}
                      hostId={selectedHostId}
                      initialPath={workspaceTrimmed}
                      onConfirm={handleWorkspaceChange}
                    />
                    <p
                      className="flex items-start gap-1.5 text-xs text-warning"
                      data-testid="switch-host-warning"
                    >
                      <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
                      <span>
                        Files don't move across hosts. Anything uncommitted stays on the old
                        machine, and the agent picks up in this directory instead.
                      </span>
                    </p>
                  </>
                ) : (
                  <p className="text-xs text-muted-foreground">
                    Select a host to choose a directory.
                  </p>
                )}
              </div>
            )}

            {/* A stranded failure reads as two separate outages — the old host
                "stopped working" and the new one "wouldn't connect" — so lead
                with the actual state and demote the server's reason to a
                secondary line. */}
            {error !== null && (
              <div className="flex flex-col gap-1 text-xs" data-testid="switch-host-error">
                {stranded ? (
                  <>
                    <span className="text-destructive">
                      {moveState === "stopped"
                        ? "The session stopped, but the move didn't finish. "
                        : "The session left its old host, but we could not confirm the move completed. Check its current host before retrying. "}
                      Pick a host — including the one it came from — and try again.
                    </span>
                    <span className="text-muted-foreground">{error}</span>
                  </>
                ) : (
                  <span className="text-destructive">{error}</span>
                )}
              </div>
            )}

            <DialogFooter>
              {/* `loading` overlays a centered spinner and disables the
                  button while holding its width — the shared affordance every
                  other submit button here uses. */}
              <Button
                data-testid="switch-host-button"
                disabled={!canSwitch}
                loading={submitting}
                onClick={handleSwitch}
              >
                Switch host
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
