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
import {
  composeSandboxWorkspace,
  normalizeWorkspacePath,
  sandboxRepoProblem,
} from "./NewChatDialog";
import { useHosts } from "@/hooks/useHosts";
import { useHostFilesystem } from "@/hooks/useHostFilesystem";
import { useRecentWorkspaces } from "@/hooks/useRecentWorkspaces";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { sandboxOptionLabel, sandboxProviderOptions } from "@/lib/capabilities";
import { sandboxHostChoice, sandboxHostChoiceProvider } from "@/lib/hostPreferences";
import { randomUUID } from "@/lib/randomUUID";
import { launchManagedHost, launchRunner, updateSession } from "@/lib/sessionsApi";
import { terminalsQueryKey, type TerminalInfo } from "@/lib/terminals";
import { useChatStore } from "@/store/chatStore";

/**
 * Move an existing session onto a different host, from the composer's
 * host badge (see ``HostBadge``).
 *
 * There is no single "switch host" endpoint — the move is the same two
 * calls the CLI's daemon-launch path already makes:
 *
 * 1. ``PATCH /v1/sessions/{id}`` with ``runner_id: ""`` releases the
 *    current runner binding. The launch endpoint binds atomically with
 *    ``UPDATE … WHERE runner_id IS NULL``, so a bound session is
 *    rejected until this clears it. The same call clears the model
 *    override, which is bound to the old host's catalog.
 * 2. ``POST /v1/hosts/{new}/runners`` validates the directory against
 *    the agent's sandbox boundary, binds the runner, and repoints the
 *    session's ``host_id``.
 *
 * A directory is always required: workspaces are per-machine, so the
 * old path means nothing on the new host even when it reads the same.
 *
 * When ``/v1/info`` reports ``managed_sandboxes_enabled``, the picker
 * also offers a new server-provisioned sandbox per provider. Step 2 is
 * then ``POST /v1/sessions/{id}/managed-host``, which returns as soon as
 * the launch is scheduled; the session page follows its progress. The
 * sandbox's workspace is a repository the server clones into it.
 *
 * The first available host is selected on open and its directory
 * defaulted, so the common "just move it over there" case is one click.
 * A sandbox costs a fresh provision, so it is the default only when no
 * other host is online.
 *
 * Step 1 landing without step 2 (a dropped connection between them)
 * leaves the session bound to nothing. The dialog stays open on that
 * failure and puts the origin host back in the picker, so recovering —
 * onward to the new host, or back to the old one — is the same click.
 *
 * @param open - Whether the dialog is visible.
 * @param onOpenChange - Radix-controlled visibility setter.
 * @param sessionId - The session to move, e.g. ``"conv_abc"``.
 * @param currentHostId - The host it is bound to now; excluded from the
 *   picker and used for the "different machine" warning.
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
  // Set when step 1 succeeded but step 2 failed: the session is now
  // unbound, so the retry copy has to say so rather than implying the
  // old host is still serving it.
  const [stranded, setStranded] = useState(false);

  // Sandbox rows appear only when the server can launch a sandbox and move a
  // session onto it; "loading" keeps them hidden until the boot probe resolves.
  const info = useServerInfo();
  const sandboxProviderRows = useMemo(
    () =>
      info !== "loading" && info.managed_sandboxes_enabled && info.move_to_managed_sandbox
        ? sandboxProviderOptions(info)
        : [],
    [info],
  );

  const { recent, addRecent } = useRecentWorkspaces(selectedHostId);

  // Pinned at mount — the badge mounts this dialog only while it is open.
  // A successful switch repoints the session's host, so filtering on the
  // live prop would drop the just-picked host out of its own list mid-
  // submit and blank the select, which renders the chosen item's label.
  const [originHostId] = useState(currentHostId);

  // Only online hosts can accept a runner, and moving to the host the
  // session is already on is a no-op — until a half-finished switch
  // leaves it unbound, when the origin is a target like any other and
  // moving back is the quickest way out.
  const targetHosts = useMemo(
    () =>
      (hosts ?? []).filter(
        (h) => h.status === "online" && (stranded || h.host_id !== originHostId),
      ),
    [hosts, originHostId, stranded],
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
      setStranded(false);
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
  const canSwitch = sandboxSelected
    ? repoProblem === null
    : selectedHostId !== null && workspaceValid;

  async function handleSwitch(): Promise<void> {
    if (!canSwitch) return;
    setSubmitting(true);
    setError(null);
    try {
      // Drop the model override alongside the runner binding. A model id is
      // host-bound — the catalog depends on that machine's credentials — so
      // carrying it over lands the next turn on "model may not exist or you
      // may not have access to it". Cleared, the new runner resolves the
      // harness default, which is by definition available there. `silent`
      // keeps this off the transcript: it's part of the move, not a /model.
      await updateSession(sessionId, { runnerId: "", modelOverride: null, silent: true });
      // From here the session is unbound until the launch lands.
      setStranded(true);
      if (sandboxSelected) {
        // The session's sandbox launch events report progress from here, so
        // nothing needs marking: the page shows the provisioning sandbox.
        await launchManagedHost(sessionId, {
          provider: sandboxProvider,
          workspace: composeSandboxWorkspace(sandboxRepoUrl, sandboxRepoBranch) ?? null,
          // Route the launch by the new sandbox's host id.
          ...(info !== "loading" && info.request_managed_sandbox_host_id
            ? { hostId: randomUUID().replace(/-/g, "") }
            : {}),
        });
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
                  {/* New sandboxes first, above the connected hosts, as in the
                      fork dialog. */}
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
                    aria-invalid={repoProblem !== null || undefined}
                    className="rounded-md border border-input aria-invalid:border-destructive bg-background px-3 py-2 font-mono text-xs outline-none transition-colors focus-visible:border-ring"
                  />
                  {repoProblem !== null ? (
                    <p
                      role="alert"
                      className="text-xs text-destructive"
                      data-testid="switch-host-sandbox-repo-error"
                    >
                      {repoProblem}
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
                    className="rounded-md border border-input bg-background px-3 py-2 font-mono text-xs outline-none transition-colors focus-visible:border-ring"
                  />
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
                      The session left its old host but didn't start on the new one, so it isn't
                      running anywhere. Pick a host — including the one it came from — and try
                      again.
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
