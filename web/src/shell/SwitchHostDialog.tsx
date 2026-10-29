import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangleIcon } from "lucide-react";
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
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { WorkspacePickerDialog } from "./WorkspacePickerDialog";
import { WorkspacePathField } from "./WorkspacePathField";
import { HostLabel } from "./HostLabel";
import { useExecutionTopology } from "@/hooks/useExecutionTopology";
import { useHosts } from "@/hooks/useHosts";
import { sessionSwitchHarness } from "@/lib/sessionSwitch";
import { getSessionSlim, launchRunner, updateSession } from "@/lib/sessionsApi";
import { terminalsQueryKey, type TerminalInfo } from "@/lib/terminals";
import { useChatStore } from "@/store/chatStore";
import {
  returnFocusOnClose,
  SwitchAccountDialog,
  switchSnapshotKey,
  type SwitchDialogProps,
} from "./SwitchAccountDialog";
import { useSwitchWorkspace } from "./useSwitchWorkspace";

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
 * old path may mean nothing on the new host. The session's own directory
 * is still the default when the new host has it.
 *
 * The first available host is selected on open and its directory
 * defaulted, so the common "just move it over there" case is one click.
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
 * @param currentWorkspace - Its directory now, e.g. ``"/home/me/repo"``.
 * @param returnFocusRef - Element to focus on close when the opener isn't a
 *   Radix trigger.
 */
export function SwitchHostDialog(props: SwitchDialogProps) {
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
  return <SwitchByAccountRegistry {...props} />;
}

/** Native sessions on an account registry get the account dialog; everything else moves by host. */
function SwitchByAccountRegistry(props: SwitchDialogProps) {
  const { open, sessionId } = props;
  const topology = useExecutionTopology(open, { live: true });
  const configured = topology.data?.configured === true;
  const pausePollingRef = useRef(false);
  // The shared ["session", id] snapshot is cached forever, so read a fresh one.
  const snapshotQuery = useQuery({
    queryKey: switchSnapshotKey(sessionId),
    queryFn: () => getSessionSlim(sessionId),
    enabled: open && configured,
    staleTime: 0,
    gcTime: 0,
    retry: false,
    // Polled while open (a finished turn shows up); paused mid-apply.
    refetchInterval: () => (pausePollingRef.current ? false : 2_000),
  });

  // The plain host move covers a server without a registry, and the moment
  // before the first registry answer arrives.
  const snapshot = snapshotQuery.data;
  if (topology.data === undefined && !topology.isError) {
    return <HostOnlySwitchDialog {...props} />;
  }
  if (topology.data === undefined) {
    return (
      <SwitchStatusDialog
        {...props}
        message="Couldn't load machines and accounts."
        onRetry={() => void topology.refetch()}
      />
    );
  }
  if (!configured) return <HostOnlySwitchDialog {...props} />;
  if (snapshot === undefined) {
    return (
      <SwitchStatusDialog
        {...props}
        message={snapshotQuery.isError ? "Couldn't read this session's current state." : undefined}
        onRetry={snapshotQuery.isError ? () => void snapshotQuery.refetch() : undefined}
      />
    );
  }
  const nativeHarness = sessionSwitchHarness(snapshot);
  // Saved inference profiles own their catalog, and sub-agents can't switch.
  const switchable = !snapshot.inferenceConfigured && snapshot.parentSessionId == null;
  if (nativeHarness === null || !switchable) {
    // A registered harness may only move to registered hosts.
    const registeredHostIds =
      nativeHarness === null
        ? undefined
        : new Set(
            topology.data.bindings.filter((b) => b.harness === nativeHarness).map((b) => b.host_id),
          );
    return <HostOnlySwitchDialog {...props} registeredHostIds={registeredHostIds} />;
  }
  return (
    <SwitchAccountDialog
      {...props}
      snapshot={snapshot}
      harness={nativeHarness}
      topology={topology.data}
      pausePollingRef={pausePollingRef}
      recheckSnapshot={async () => (await snapshotQuery.refetch()).data}
    />
  );
}

/** Loading (no `message`) or load-failure (`message` and `onRetry`) state for the configured registry. */
function SwitchStatusDialog({
  open,
  onOpenChange,
  message,
  onRetry,
}: SwitchDialogProps & { message?: string; onRetry?: () => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid="switch-host-dialog" className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Switch model or account</DialogTitle>
          <DialogDescription role={onRetry ? "alert" : "status"}>
            {message ?? "Loading…"}
          </DialogDescription>
        </DialogHeader>
        {onRetry && (
          <DialogFooter data-testid="switch-load-error">
            <Button variant="outline" onClick={onRetry}>
              Try again
            </Button>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  );
}

function HostOnlySwitchDialog({
  open,
  onOpenChange,
  sessionId,
  currentHostId,
  currentWorkspace = null,
  returnFocusRef,
  registeredHostIds,
}: SwitchDialogProps & {
  /** When set, only these hosts are offered. */
  registeredHostIds?: ReadonlySet<string>;
}) {
  const queryClient = useQueryClient();
  const { data: hosts } = useHosts({ enabled: open });
  const markRunnerLaunched = useChatStore((s) => s.markRunnerLaunched);

  const [selectedHostId, setSelectedHostId] = useState<string | null>(null);
  const [browsing, setBrowsing] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Set when step 1 succeeded but step 2 failed: the session is now
  // unbound, so the retry copy has to say so rather than implying the
  // old host is still serving it.
  const [stranded, setStranded] = useState(false);

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
        (h) =>
          h.status === "online" &&
          (stranded || h.host_id !== originHostId) &&
          (registeredHostIds === undefined || registeredHostIds.has(h.host_id)),
      ),
    [hosts, originHostId, stranded, registeredHostIds],
  );

  // Land on a usable selection instead of an empty form: take the first
  // available host as soon as the list arrives.
  useEffect(() => {
    if (open && selectedHostId === null && targetHosts.length > 0) {
      setSelectedHostId(targetHosts[0].host_id);
    }
  }, [open, selectedHostId, targetHosts]);

  const directory = useSwitchWorkspace({ open, hostId: selectedHostId, currentWorkspace });
  const { path: workspaceTrimmed, valid: workspaceValid, addRecent } = directory;

  function handleOpenChange(next: boolean): void {
    if (!next) {
      setSelectedHostId(null);
      directory.reset();
      setBrowsing(false);
      setError(null);
      setStranded(false);
      setSubmitting(false);
    }
    onOpenChange(next);
  }

  function commitWorkspacePath(path: string): void {
    directory.onChange(path);
    setBrowsing(true);
  }

  async function handleSwitch(): Promise<void> {
    if (!selectedHostId || !workspaceValid) return;
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
      await launchRunner(selectedHostId, sessionId, workspaceTrimmed);
      // The new runner is booting but nothing on the wire says so yet, and
      // no turn is in flight to imply it — without this the session reads as
      // idle and the move lands on a silent, empty chat.
      markRunnerLaunched();
      addRecent(workspaceTrimmed);
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

  const noTargets = hosts !== undefined && targetHosts.length === 0;

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        data-testid="switch-host-dialog"
        onCloseAutoFocus={returnFocusOnClose(returnFocusRef)}
        className="flex flex-col gap-4 sm:max-w-lg"
      >
        <DialogHeader>
          <DialogTitle>Switch host</DialogTitle>
          <DialogDescription>Move this session to another connected machine.</DialogDescription>
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
                value={selectedHostId ?? ""}
                onValueChange={(v) => setSelectedHostId(v)}
                componentId="switch_host.host"
              >
                <SelectTrigger className="w-full text-xs" data-testid="switch-host-select">
                  <SelectValue placeholder="Select a host" />
                </SelectTrigger>
                <SelectContent>
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

            <div className="flex flex-col gap-2">
              <span className="text-xs font-medium text-muted-foreground">Working directory</span>
              {selectedHostId ? (
                <>
                  <WorkspacePathField
                    hostId={selectedHostId}
                    value={directory.workspace}
                    onChange={directory.onChange}
                    onBrowse={() => setBrowsing((v) => !v)}
                    onCommit={commitWorkspacePath}
                    recent={directory.recent}
                    dropdownDisabled={browsing}
                  />
                  <WorkspacePickerDialog
                    open={browsing}
                    onOpenChange={setBrowsing}
                    hostId={selectedHostId}
                    initialPath={workspaceTrimmed}
                    onConfirm={directory.onChange}
                  />
                  <p
                    className="flex items-start gap-1.5 text-xs text-warning"
                    data-testid="switch-host-warning"
                  >
                    <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
                    <span>
                      Files don't move across hosts. Anything uncommitted stays on the old machine,
                      and the agent picks up in this directory instead.
                    </span>
                  </p>
                </>
              ) : (
                <p className="text-xs text-muted-foreground">
                  Select a host to choose a directory.
                </p>
              )}
            </div>

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
                disabled={!selectedHostId || !workspaceValid}
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
