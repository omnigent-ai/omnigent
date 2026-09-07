import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangleIcon, GitBranchIcon } from "lucide-react";
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
import { WorkspacePicker, isNavigablePath } from "./WorkspacePicker";
import { WorkspacePathField } from "./WorkspacePathField";
import { CliCommandBlock } from "./CliCommandBlock";
import { HostLabel } from "./HostLabel";
import { buildReconnectCommand } from "./ReconnectSessionDialog";
import {
  isValidWorkspace,
  normalizeWorkspacePath,
  sessionsSharingDirectory,
} from "./NewChatDialog";
import { useHosts } from "@/hooks/useHosts";
import { useDirectorySessions } from "@/hooks/useDirectorySessions";
import { useRunnerHealthRegistration } from "@/hooks/RunnerHealthProvider";
import { useRecentWorkspaces } from "@/hooks/useRecentWorkspaces";
import { getSessionSlim, launchRunner } from "@/lib/sessionsApi";

/**
 * Dialog that binds an *unbound* session to a host + directory in-app via
 * ``POST /v1/hosts/{id}/runners`` (``launchRunner``) and lets the runner
 * start, after which ChatPage replays any queued message. Unlike
 * ``ResumeChatDialog`` (which only prints a CLI command), this resumes the
 * session from the browser. Serves two unbound cases:
 *
 * - **Fork clone** (``sourceSessionId`` set): a fork of a session that had a
 *   working directory (``omnigent.fork.source_id`` label). Prefills from the
 *   *source* session — its host is the default, its workspace the default
 *   directory, and when it used a git worktree a branch is suggested so the
 *   clone diverges onto its own worktree. When the source's host is offline
 *   there's nothing to launch on, so it falls back to the CLI reconnect
 *   command — the escape hatch ``ResumeChatDialog`` shows.
 *
 * - **Host-less session** (no ``sourceSessionId``): an imported session with
 *   no host/runner of its own. Prefills from ``prefill`` (the session's own
 *   recorded workspace/host) and defaults the host to the caller's current
 *   online machine, so the common case is one click.
 *
 * @param open - Whether the dialog is visible.
 * @param onOpenChange - Radix-controlled visibility setter.
 * @param sessionId - The unbound session to bind, e.g. ``"conv_abc"``.
 * @param sourceSessionId - Fork source (``omnigent.fork.source_id``) read for
 *   host/dir/branch prefill; ``null``/absent for a host-less session.
 * @param prefill - Defaults for the host-less case (the session's own
 *   host/workspace/branch). Ignored when ``sourceSessionId`` is set.
 * @param serverUrl - Origin for the CLI fallback command.
 * @param wrapper - The session's ``omnigent.wrapper`` label (CLI fallback).
 * @param onBound - Called after a successful bind so the caller can
 *   replay the message the user was trying to send.
 */
export function ResumeWithDirectoryDialog({
  open,
  onOpenChange,
  sessionId,
  sourceSessionId,
  prefill,
  serverUrl,
  wrapper,
  onBound,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  sessionId: string;
  sourceSessionId?: string | null;
  prefill?: { hostId?: string | null; workspace?: string | null; gitBranch?: string | null };
  serverUrl: string;
  wrapper?: string | null;
  onBound?: () => void;
}) {
  const queryClient = useQueryClient();

  // A fork clone prefills from its source session; a host-less session has no
  // source and prefills from its own recorded fields instead.
  const hasSource = sourceSessionId != null && sourceSessionId !== "";

  // Source session prefill (host/workspace/git_branch). Only fetch while
  // the dialog is open and we actually have a source.
  const { data: source, isLoading: sourceLoading } = useQuery({
    queryKey: ["session", sourceSessionId],
    queryFn: () => getSessionSlim(sourceSessionId as string),
    enabled: open && hasSource,
  });
  const { data: hosts } = useHosts({ enabled: open });

  const sourceHostId = source?.hostId ?? null;
  const sourceHost = useMemo(
    () => hosts?.find((h) => h.host_id === sourceHostId) ?? null,
    [hosts, sourceHostId],
  );
  const sourceHostOnline = sourceHost?.status === "online";
  const onlineHosts = useMemo(() => (hosts ?? []).filter((h) => h.status === "online"), [hosts]);

  // Unified prefill: a fork reads its source session; a host-less session
  // reads the ``prefill`` its own snapshot supplied.
  const prefillWorkspace = hasSource ? source?.workspace : prefill?.workspace;
  const prefillBranch = hasSource ? source?.gitBranch : prefill?.gitBranch;
  // Default host: the source's host (fork, only if online — otherwise the CLI
  // fallback fires) or, for a host-less session, its own recorded host when
  // still online, else the caller's current (most-recent) online machine.
  const defaultHostId = useMemo(() => {
    if (hasSource) return sourceHostOnline ? sourceHostId : null;
    const preferred = onlineHosts.find((h) => h.host_id === prefill?.hostId);
    return (preferred ?? onlineHosts[0])?.host_id ?? null;
  }, [hasSource, sourceHostOnline, sourceHostId, onlineHosts, prefill?.hostId]);

  const [selectedHostId, setSelectedHostId] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState("");
  const [branchName, setBranchName] = useState("");
  const [baseBranch, setBaseBranch] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const [browseNonce, setBrowseNonce] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { recent, addRecent } = useRecentWorkspaces(selectedHostId);

  // Prefill the host selection once the hosts (and source, if any) load.
  useEffect(() => {
    if (open && selectedHostId === null && defaultHostId) {
      setSelectedHostId(defaultHostId);
    }
  }, [open, selectedHostId, defaultHostId]);

  // Prefill the directory with the session's recorded workspace.
  useEffect(() => {
    if (open && workspace === "" && prefillWorkspace) {
      setWorkspace(prefillWorkspace);
    }
  }, [open, workspace, prefillWorkspace]);

  // When the source used a worktree, default the base ref to that branch
  // so the clone branches off where the original left work.
  useEffect(() => {
    if (open && baseBranch === "" && prefillBranch) {
      setBaseBranch(prefillBranch);
    }
  }, [open, baseBranch, prefillBranch]);

  // Reset transient state when the dialog closes.
  function handleOpenChange(next: boolean): void {
    if (!next) {
      setSelectedHostId(null);
      setWorkspace("");
      setBranchName("");
      setBaseBranch("");
      setBrowsing(false);
      setError(null);
      setSubmitting(false);
    }
    onOpenChange(next);
  }

  const workspaceTrimmed = normalizeWorkspacePath(workspace) ?? "";
  const workspaceValid = isValidWorkspace(workspace);

  // Conflict hint: other *connected* sessions already working in the
  // picked directory on this host (same wiring as NewChatDialog).
  const { data: directorySessions } = useDirectorySessions(open && Boolean(selectedHostId));
  const conflictCandidates = useMemo(
    () =>
      open
        ? (directorySessions ?? []).filter(
            (s) => s.host_id === selectedHostId && s.workspace != null,
          )
        : [],
    [open, directorySessions, selectedHostId],
  );
  const runnerHealth = useRunnerHealthRegistration(conflictCandidates);
  const conflictingSessions = useMemo(
    () =>
      sessionsSharingDirectory(
        conflictCandidates,
        selectedHostId,
        workspaceTrimmed,
        (id) => runnerHealth.get(id) === true,
      ),
    [conflictCandidates, selectedHostId, workspaceTrimmed, runnerHealth],
  );
  const showConflictHint = branchName.trim() === "" && conflictingSessions.length > 0;

  // Mismatched-directory warning: the transcript's file references were
  // grounded in the source's directory ON the source's host. A different
  // directory — or a different host, where even an identical path is a
  // different machine — won't resolve them, so the agent must re-orient.
  const sourceWorkspaceNorm = source?.workspace ? normalizeWorkspacePath(source.workspace) : null;
  const hostMismatch =
    sourceHostId !== null && selectedHostId !== null && selectedHostId !== sourceHostId;
  const showMismatchWarning =
    (hostMismatch && workspaceTrimmed !== "") ||
    (sourceWorkspaceNorm !== null &&
      workspaceTrimmed !== "" &&
      workspaceTrimmed !== sourceWorkspaceNorm);

  function commitWorkspacePath(path: string): void {
    setWorkspace(path);
    setBrowsing(true);
    setBrowseNonce((n) => n + 1);
  }

  async function handleBind(): Promise<void> {
    if (!selectedHostId || !workspaceValid) return;
    setSubmitting(true);
    setError(null);
    try {
      const trimmedBranch = branchName.trim();
      await launchRunner(
        selectedHostId,
        sessionId,
        workspaceTrimmed,
        trimmedBranch
          ? { branchName: trimmedBranch, baseBranch: baseBranch.trim() || undefined }
          : undefined,
      );
      // The clone is now bound + launching; refresh the session list so
      // its host/runner binding (and online dot) update.
      addRecent(workspaceTrimmed);
      await queryClient.invalidateQueries({ queryKey: ["conversations"] });
      handleOpenChange(false);
      onBound?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't start the session. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  // No runner can be launched when the source's host is offline (CUJ 1
  // same-user resumes on the original host). Surface the reconnect
  // command instead — once the host is back, retrying opens the picker.
  // Gate on the hosts list having loaded: until then `sourceHostOnline`
  // is falsy only because we don't KNOW the host's status yet, and
  // flashing the CLI fallback for an online source host would be wrong.
  const hostsLoaded = hosts !== undefined;
  const showCliFallback = !sourceLoading && hostsLoaded && source != null && !sourceHostOnline;
  const loading = (hasSource && sourceLoading) || !hostsLoaded;
  // Host-less sessions have no source host to reconnect, so there's no CLI
  // fallback: when the caller owns no online machine, say so plainly.
  const noOnlineHosts = !hasSource && hostsLoaded && onlineHosts.length === 0;

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent data-testid="resume-dir-dialog" className="flex flex-col gap-4 sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{hasSource ? "Resume this session" : "Resume on a machine"}</DialogTitle>
          <DialogDescription>
            {hasSource
              ? "This clone hasn't picked a working directory yet. Choose a host and directory to continue the conversation against your files."
              : "This session isn't running on any machine. Pick one of your machines and a directory to continue the conversation against your files."}
          </DialogDescription>
        </DialogHeader>

        {loading ? (
          <p className="text-sm text-muted-foreground" data-testid="resume-dir-loading">
            {hasSource ? "Loading the original session's directory…" : "Loading your machines…"}
          </p>
        ) : noOnlineHosts ? (
          <p className="text-sm text-muted-foreground" data-testid="resume-dir-no-hosts">
            None of your machines are online. Start one with{" "}
            <code className="rounded bg-muted px-1 py-0.5 font-mono">omnigent host</code> from your
            terminal, then reopen this dialog.
          </p>
        ) : showCliFallback ? (
          <div className="flex flex-col gap-2" data-testid="resume-dir-cli-fallback">
            <p className="text-sm text-muted-foreground">
              The original session's host is offline, so there's nothing to launch a runner on.
              Reconnect it from your terminal — then send your message again to pick a directory.
            </p>
            <CliCommandBlock
              command={buildReconnectCommand({
                conversationId: sessionId,
                serverUrl,
                wrapper,
                // The source's host is offline here. With a host binding the
                // owner re-registers the host (`omnigent host`); without one
                // the runner is relaunched directly via the wrapper's resume
                // form.
                state: sourceHostId ? "host_offline" : "local_stranded",
              })}
              testIdPrefix="resume-dir"
            />
          </div>
        ) : (
          <>
            <div className="flex flex-col gap-2">
              <span className="text-sm font-medium text-muted-foreground">Host</span>
              <Select value={selectedHostId ?? ""} onValueChange={(v) => setSelectedHostId(v)}>
                <SelectTrigger className="w-full text-sm" data-testid="resume-dir-host-select">
                  <SelectValue placeholder="Select a host" />
                </SelectTrigger>
                <SelectContent>
                  {onlineHosts.map((host) => (
                    <SelectItem
                      key={host.host_id}
                      value={host.host_id}
                      data-testid={`resume-dir-host-option-${host.host_id}`}
                    >
                      <HostLabel host={host} />
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="flex flex-col gap-2">
              <span className="text-sm font-medium text-muted-foreground">Working directory</span>
              {selectedHostId ? (
                <>
                  <WorkspacePathField
                    hostId={selectedHostId}
                    value={workspace}
                    onChange={setWorkspace}
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
                        setWorkspace(path);
                        setBrowsing(false);
                      }}
                      onClose={() => setBrowsing(false)}
                    />
                  )}
                  {showConflictHint && (
                    <p
                      className="flex items-start gap-1.5 text-sm text-warning"
                      data-testid="resume-dir-conflict-hint"
                    >
                      <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
                      <span>
                        {conflictingSessions.length === 1
                          ? "1 other agent is"
                          : `${conflictingSessions.length} other agents are`}{" "}
                        working in this directory. Write operations may conflict. Name a git branch
                        below to work in an isolated copy.
                      </span>
                    </p>
                  )}
                  {showMismatchWarning && (
                    <p
                      className="flex items-start gap-1.5 text-sm text-warning"
                      data-testid="resume-dir-mismatch-warning"
                    >
                      <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
                      <span>
                        This directory differs from the original session's. Earlier file references
                        in the transcript may not apply — the agent will need to re-orient.
                      </span>
                    </p>
                  )}
                </>
              ) : (
                <p className="text-sm text-muted-foreground">
                  Select a host to choose a directory.
                </p>
              )}
            </div>

            <div className="flex flex-col gap-1">
              <label
                htmlFor="resume-dir-branch"
                className="flex items-center gap-1.5 text-sm font-medium text-muted-foreground"
              >
                <GitBranchIcon className="size-3.5" />
                Git worktree (optional)
              </label>
              <input
                id="resume-dir-branch"
                type="text"
                value={branchName}
                onChange={(e) => setBranchName(e.target.value)}
                placeholder="feature/my-branch"
                data-testid="resume-dir-branch-input"
                className="rounded-md border border-input bg-background px-3 py-2 font-mono text-sm outline-none transition-colors focus-visible:border-ring"
              />
              {branchName.trim() !== "" && (
                <input
                  id="resume-dir-base-branch"
                  type="text"
                  value={baseBranch}
                  onChange={(e) => setBaseBranch(e.target.value)}
                  placeholder="Base branch (defaults to the current branch)"
                  aria-label="Base branch"
                  data-testid="resume-dir-base-branch-input"
                  className="rounded-md border border-input bg-background px-3 py-2 font-mono text-sm outline-none transition-colors focus-visible:border-ring"
                />
              )}
              <p className="text-sm text-muted-foreground">
                Creates a git worktree for a new branch in an isolated directory — keeps the clone
                from fighting the original over the same files. Leave blank to start in the picked
                directory.
              </p>
            </div>

            {error !== null && (
              <p className="text-sm text-destructive" data-testid="resume-dir-error">
                {error}
              </p>
            )}

            <DialogFooter>
              <Button
                data-testid="resume-dir-bind-button"
                loading={submitting}
                disabled={!selectedHostId || !workspaceValid}
                onClick={handleBind}
              >
                Start session
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
