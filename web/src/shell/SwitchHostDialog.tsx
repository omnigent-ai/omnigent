import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { WorkspacePicker, isNavigablePath, parentOf } from "./WorkspacePicker";
import { WorkspacePathField } from "./WorkspacePathField";
import { HostLabel } from "./HostLabel";
import { isValidWorkspace, normalizeWorkspacePath } from "./NewChatDialog";
import { useHosts } from "@/hooks/useHosts";
import { useHostFilesystem } from "@/hooks/useHostFilesystem";
import { useRecentWorkspaces } from "@/hooks/useRecentWorkspaces";
import { launchRunner, updateSession } from "@/lib/sessionsApi";
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
 */
export function SwitchHostDialog({
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
  const [workspace, setWorkspace] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const [browseNonce, setBrowseNonce] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Set when step 1 succeeded but step 2 failed: the session is now
  // unbound, so the retry copy has to say so rather than implying the
  // old host is still serving it.
  const [stranded, setStranded] = useState(false);

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
  // available host as soon as the list arrives.
  useEffect(() => {
    if (open && selectedHostId === null && targetHosts.length > 0) {
      setSelectedHostId(targetHosts[0].host_id);
    }
  }, [open, selectedHostId, targetHosts]);

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

  const workspaceTrimmed = normalizeWorkspacePath(workspace) ?? "";
  const workspaceValid = isValidWorkspace(workspace);

  function commitWorkspacePath(path: string): void {
    handleWorkspaceChange(path);
    setBrowsing(true);
    setBrowseNonce((n) => n + 1);
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
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't switch hosts. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  const noTargets = hosts !== undefined && targetHosts.length === 0;

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent data-testid="switch-host-dialog" className="flex flex-col gap-4 sm:max-w-lg">
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
                    value={workspace}
                    onChange={handleWorkspaceChange}
                    onBrowse={() => setBrowsing((v) => !v)}
                    onCommit={commitWorkspacePath}
                    recent={recent}
                    dropdownDisabled={browsing}
                  />
                  {browsing && (
                    <WorkspacePicker
                      key={browseNonce}
                      hostId={selectedHostId}
                      initialPath={isNavigablePath(workspaceTrimmed) ? workspaceTrimmed : undefined}
                      onSelect={(path) => {
                        handleWorkspaceChange(path);
                        setBrowsing(false);
                      }}
                      onClose={() => setBrowsing(false)}
                    />
                  )}
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
