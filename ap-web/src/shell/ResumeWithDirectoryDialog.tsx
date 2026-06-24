import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangleIcon, MonitorCloudIcon, GitBranchIcon, MonitorIcon } from "lucide-react";
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
import { buildReconnectCommand } from "./ReconnectSessionDialog";
import {
  isValidWorkspace,
  normalizeWorkspacePath,
  sessionsSharingDirectory,
} from "./NewChatDialog";
import { useHosts, type Host } from "@/hooks/useHosts";
import { useDirectorySessions } from "@/hooks/useDirectorySessions";
import { useRunnerHealthRegistration } from "@/hooks/RunnerHealthProvider";
import { useRecentWorkspaces } from "@/hooks/useRecentWorkspaces";
import { getSessionSlim, launchRunner } from "@/lib/sessionsApi";

/**
 * Compact host label for the Select item — mirrors NewChatDialog's
 * HostOption (which is private to that module).
 */
function HostLabel({ host }: { host: Host }) {
  const isOnline = host.status === "online";
  return (
    <span className="flex items-center gap-2">
      {host.name.toLowerCase().includes("cloud") ? (
        <MonitorCloudIcon className="size-4 text-muted-foreground" />
      ) : (
        <MonitorIcon className="size-4 text-muted-foreground" />
      )}
      <span className="font-mono text-xs">{host.name}</span>
      <span
        className={`inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider ${
          isOnline ? "text-green-600" : "text-muted-foreground"
        }`}
      >
        <span
          className={`inline-block size-1.5 rounded-full ${isOnline ? "bg-green-500" : "bg-muted-foreground"}`}
        />
        {host.status}
      </span>
    </span>
  );
}

/**
 * Dialog surfaced when the user tries to chat with an unbound *coding*
 * clone (a fork of a session that had a working directory — it carries
 * the ``omnigent.fork.source_id`` label). Unlike ``ResumeChatDialog``
 * (which only prints a CLI command), this binds the clone to a host +
 * directory in-app via ``POST /v1/hosts/{id}/runners`` (``launchRunner``)
 * and lets the runner start, after which ChatPage replays the queued
 * message.
 *
 * The picker prefills from the *source* session (same-user CUJ 1):
 * the source's host is the default, its workspace the default directory,
 * and — when the source used a git worktree — a branch is suggested so
 * the clone diverges onto its own worktree rather than fighting the
 * original over the same files.
 *
 * When the source's host is offline there is no runner to launch, so the
 * dialog falls back to the CLI reconnect command (``omnigent connect``)
 * — the same escape hatch ``ResumeChatDialog`` shows.
 *
 * @param open - Whether the dialog is visible.
 * @param onOpenChange - Radix-controlled visibility setter.
 * @param sessionId - The unbound clone to bind, e.g. ``"conv_clone"``.
 * @param sourceSessionId - The source the clone was forked from
 *   (``omnigent.fork.source_id``); read for host/dir/branch prefill.
 * @param serverUrl - Origin for the CLI fallback command.
 * @param wrapper - The clone's ``omnigent.wrapper`` label (CLI fallback).
 * @param onBound - Called after a successful bind so the caller can
 *   replay the message the user was trying to send.
 */
export function ResumeWithDirectoryDialog({
  open,
  onOpenChange,
  sessionId,
  sourceSessionId,
  serverUrl,
  wrapper,
  onBound,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  sessionId: string;
  sourceSessionId: string;
  serverUrl: string;
  wrapper?: string | null;
  onBound?: () => void;
}) {
  const { t } = useTranslation("common");
  const queryClient = useQueryClient();

  // Source session prefill (host/workspace/git_branch). Only fetch while
  // the dialog is open.
  const { data: source, isLoading: sourceLoading } = useQuery({
    queryKey: ["session", sourceSessionId],
    queryFn: () => getSessionSlim(sourceSessionId),
    enabled: open,
  });
  const { data: hosts } = useHosts({ enabled: open });

  const sourceHostId = source?.hostId ?? null;
  const sourceHost = useMemo(
    () => hosts?.find((h) => h.host_id === sourceHostId) ?? null,
    [hosts, sourceHostId],
  );
  const sourceHostOnline = sourceHost?.status === "online";
  const onlineHosts = useMemo(() => (hosts ?? []).filter((h) => h.status === "online"), [hosts]);

  const [selectedHostId, setSelectedHostId] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState("");
  const [branchName, setBranchName] = useState("");
  const [baseBranch, setBaseBranch] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const [browseNonce, setBrowseNonce] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { recent, addRecent } = useRecentWorkspaces(selectedHostId);

  // Prefill host = source host (when it is online) once both load.
  useEffect(() => {
    if (open && selectedHostId === null && sourceHostId && sourceHostOnline) {
      setSelectedHostId(sourceHostId);
    }
  }, [open, selectedHostId, sourceHostId, sourceHostOnline]);

  // Prefill the directory with the source's workspace.
  useEffect(() => {
    if (open && workspace === "" && source?.workspace) {
      setWorkspace(source.workspace);
    }
  }, [open, workspace, source?.workspace]);

  // When the source used a worktree, default the base ref to that branch
  // so the clone branches off where the original left work.
  useEffect(() => {
    if (open && baseBranch === "" && source?.gitBranch) {
      setBaseBranch(source.gitBranch);
    }
  }, [open, baseBranch, source?.gitBranch]);

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
      setError(e instanceof Error ? e.message : t("couldntStartSession"));
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

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent data-testid="resume-dir-dialog" className="flex flex-col gap-4 sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{t("resumeThisSession")}</DialogTitle>
          <DialogDescription>{t("resumeThisSessionDesc")}</DialogDescription>
        </DialogHeader>

        {sourceLoading || !hostsLoaded ? (
          <p className="text-xs text-muted-foreground" data-testid="resume-dir-loading">
            {t("loadingOriginalDirectory")}
          </p>
        ) : showCliFallback ? (
          <div className="flex flex-col gap-2" data-testid="resume-dir-cli-fallback">
            <p className="text-xs text-muted-foreground">{t("resumeHostOfflineHint")}</p>
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
              <span className="text-xs font-medium text-muted-foreground">{t("host")}</span>
              <Select value={selectedHostId ?? ""} onValueChange={(v) => setSelectedHostId(v)}>
                <SelectTrigger className="w-full text-xs" data-testid="resume-dir-host-select">
                  <SelectValue placeholder={t("selectAHost")} />
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
              <span className="text-xs font-medium text-muted-foreground">
                {t("workingDirectory")}
              </span>
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
                      className="flex items-start gap-1.5 text-xs text-warning"
                      data-testid="resume-dir-conflict-hint"
                    >
                      <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
                      <span>{t("conflictHintBelow", { count: conflictingSessions.length })}</span>
                    </p>
                  )}
                  {showMismatchWarning && (
                    <p
                      className="flex items-start gap-1.5 text-xs text-warning"
                      data-testid="resume-dir-mismatch-warning"
                    >
                      <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
                      <span>{t("directoryMismatchWarning")}</span>
                    </p>
                  )}
                </>
              ) : (
                <p className="text-xs text-muted-foreground">{t("selectHostToChooseDir")}</p>
              )}
            </div>

            <div className="flex flex-col gap-1">
              <label
                htmlFor="resume-dir-branch"
                className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground"
              >
                <GitBranchIcon className="size-3.5" />
                {t("gitWorktreeOptional")}
              </label>
              <input
                id="resume-dir-branch"
                type="text"
                value={branchName}
                onChange={(e) => setBranchName(e.target.value)}
                placeholder={t("branchPlaceholderExample")}
                data-testid="resume-dir-branch-input"
                className="rounded-md border border-input bg-background px-3 py-2 font-mono text-xs outline-none transition-colors focus-visible:border-ring"
              />
              {branchName.trim() !== "" && (
                <input
                  id="resume-dir-base-branch"
                  type="text"
                  value={baseBranch}
                  onChange={(e) => setBaseBranch(e.target.value)}
                  placeholder={t("baseBranchPlaceholderCurrent")}
                  aria-label={t("baseBranch")}
                  data-testid="resume-dir-base-branch-input"
                  className="rounded-md border border-input bg-background px-3 py-2 font-mono text-xs outline-none transition-colors focus-visible:border-ring"
                />
              )}
              <p className="text-xs text-muted-foreground">{t("gitWorktreeCloneHelp")}</p>
            </div>

            {error !== null && (
              <p className="text-xs text-destructive" data-testid="resume-dir-error">
                {error}
              </p>
            )}

            <DialogFooter>
              <Button
                data-testid="resume-dir-bind-button"
                disabled={!selectedHostId || !workspaceValid || submitting}
                onClick={handleBind}
              >
                {submitting ? t("starting") : t("startSession")}
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
