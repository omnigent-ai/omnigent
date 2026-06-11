import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "@/lib/routing";
import { useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangleIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  MonitorCloudIcon,
  GitBranchIcon,
  InfoIcon,
  MonitorIcon,
} from "lucide-react";
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
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { forkSession, launchRunner } from "@/lib/sessionsApi";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useSessionAgent } from "@/hooks/useAgents";
import { useHosts, type Host } from "@/hooks/useHosts";
import { useDirectorySessions } from "@/hooks/useDirectorySessions";
import { useRunnerHealthRegistration } from "@/hooks/RunnerHealthProvider";
import { useRecentWorkspaces } from "@/hooks/useRecentWorkspaces";
import { forkTargetCarriesHistory } from "@/lib/forkHarness";
import { getCliServerUrl } from "@/lib/host";
import { WorkspacePicker, isNavigablePath } from "./WorkspacePicker";
import { WorkspacePathField } from "./WorkspacePathField";
import {
  ConnectHostInstructions,
  isValidWorkspace,
  normalizeWorkspacePath,
  sessionsSharingDirectory,
} from "./NewChatDialog";

// Select sentinel for "keep the source's agent" (Radix Select needs a
// non-empty value). When chosen, the fork omits agent_id and the server
// clones the source's agent.
const SAME_AS_SOURCE = "__same__";

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
 * Prefill for the fork's title input. Mirrors the server's
 * `"Fork of <title>"` derivation when the source has a title; when it
 * doesn't, returns "" so submitting omits the title and the server
 * derives it (rather than inventing a client-side placeholder).
 */
function defaultForkTitle(sourceTitle: string | null | undefined): string {
  const trimmed = sourceTitle?.trim();
  return trimmed ? `Fork of ${trimmed}` : "";
}

/**
 * Clone/fork form — the single "fork + start" implementation, embedded
 * by {@link ForkSessionDialog} (the header-menu Clone dialog) and by the
 * ReconnectSessionDialog's Clone tab. Renders the scrollable field stack
 * plus its own footer (Cancel / Clone); the host dialog provides the
 * surrounding `DialogContent` and header.
 *
 * Forks the active (top-level) session via ``POST /v1/sessions/{id}/fork``
 * (the server deep-copies the transcript and clones the agent into a fresh
 * session owned by the caller; comments and permissions are NOT copied and
 * future messages don't mutate the source). For a *coding* source (one with
 * a working directory), the form also picks a host + directory + optional
 * git worktree and binds the fork to a runner via ``launchRunner``
 * (``POST /v1/hosts/{id}/runners``). For a non-coding source there is no
 * directory to pick, so it forks with just name + agent.
 *
 * The fork call is the only thing the form awaits: on success it closes and
 * navigates into the clone IMMEDIATELY, and (for a coding source) fires the
 * runner launch in the background. Holding the dialog through the launch
 * blocks for as long as a worktree create takes (up to minutes) and hangs
 * forever on a dropped response, so the launch is detached. If it fails the
 * clone stays unbound and the user retries the bind via the session page's
 * directory picker (ChatPage's existing unbound-fork path). A fork-call
 * failure (nothing created) surfaces inline and the inputs stay editable for
 * a straight resubmit.
 *
 * Host/dir prefill from the *source*: its host is the default (when online),
 * its workspace the default directory, and — when the source used a worktree
 * — its branch the default base ref. The Fork button greys out until a valid
 * online host + directory are chosen (no CLI fallback).
 *
 * All form state lives here, inside the dialog content, so closing the
 * dialog unmounts the form and resets it — no manual reset needed.
 *
 * @param sourceSessionId - Session being forked.
 * @param sourceTitle - Source title, used to prefill the fork's name.
 * @param sourceWorkspace - Source workspace; presence marks a coding source
 *   (shows the host/dir fields) and seeds the directory default.
 * @param sourceHostId - Source host; default host when it is online.
 * @param sourceGitBranch - Source git branch; seeds the worktree base ref.
 * @param upToResponseId - Truncation point for a "fork from here": the
 *   fork copies history only up to and including this response. `null` /
 *   omitted forks the full history.
 * @param onClose - Closes the host dialog (Cancel, and after a
 *   successful fork).
 */
export function ForkSessionForm({
  sourceSessionId,
  sourceTitle,
  sourceWorkspace,
  sourceHostId,
  sourceGitBranch,
  upToResponseId,
  onClose,
}: {
  sourceSessionId: string;
  sourceTitle?: string | null;
  sourceWorkspace?: string | null;
  sourceHostId?: string | null;
  sourceGitBranch?: string | null;
  upToResponseId?: string | null;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  // Name is optional — left blank, the server derives "Fork of <source
  // title>" (shown as the input's placeholder). So the field starts empty.
  const [title, setTitle] = useState("");
  const [agentChoice, setAgentChoice] = useState<string>(SAME_AS_SOURCE);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Working directory + git worktree live behind "Advanced settings",
  // collapsed by default (they prefill sensibly from the source, so the
  // common "clone & start in the same place" path needs no input).
  const [showAdvanced, setShowAdvanced] = useState(false);
  // Auto-expands Advanced once when a directory conflict is detected, so the
  // warning + branch field aren't hidden. A ref (not state in the effect dep)
  // keeps it one-shot — the user can re-collapse it without it springing back.
  const autoExpandedRef = useRef(false);

  // A coding source ran in a working directory; only then does the fork
  // need a host + directory to start. A non-coding source forks with just
  // name + agent (no directory to pick).
  const isCodingSource = Boolean(sourceWorkspace);

  // Host/dir/worktree state — only meaningful for a coding source.
  const [selectedHostId, setSelectedHostId] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState("");
  const [branchName, setBranchName] = useState("");
  const [baseBranch, setBaseBranch] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const [browseNonce, setBrowseNonce] = useState(0);
  // Whether the "connect another host" CLI hint is expanded (only shown when
  // at least one host is online; otherwise the instructions render directly).
  const [showConnect, setShowConnect] = useState(false);

  // Built-in agents to switch to. The source session's bound agent gives
  // us its harness so we can offer only the targets that preserve
  // conversation history. (The form only mounts while its dialog is open,
  // so no extra enabled-gating on visibility is needed.)
  const { data: agents } = useAvailableAgents({ enabled: true });
  const { data: sourceAgent } = useSessionAgent(sourceSessionId);

  // Hosts for the picker — only for a coding source (a non-coding fork
  // never shows the host field).
  const { data: hosts } = useHosts({ enabled: isCodingSource });
  const allHosts = hosts ?? [];
  const onlineHosts = useMemo(() => (hosts ?? []).filter((h) => h.status === "online"), [hosts]);
  const offlineHosts = useMemo(() => (hosts ?? []).filter((h) => h.status === "offline"), [hosts]);
  const sourceHostOnline = onlineHosts.some((h) => h.host_id === sourceHostId);
  const serverUrl = getCliServerUrl();

  const { recent, addRecent } = useRecentWorkspaces(selectedHostId);

  // Whether the picked host is the SAME machine the source ran on. Only then
  // does "reuse the source's working directory" make sense — on a different
  // host that path is on someone else's machine. Drives the dir prefill, the
  // reuse-dir indicator, and whether Advanced starts collapsed.
  const onSourceHost = isCodingSource && selectedHostId !== null && selectedHostId === sourceHostId;
  const onDifferentHost =
    isCodingSource && selectedHostId !== null && selectedHostId !== sourceHostId;

  // The source's bound agent, stripped of any " (fork <id>)" suffix the
  // fork route appends when cloning an agent. A fork-of-a-fork's source
  // agent is named e.g. "databricks_coding_agent (fork ag_5c78e6a)", which
  // wouldn't match the built-in "databricks_coding_agent" by name — strip
  // the suffix so the dedup below still hides it.
  const sourceAgentName = sourceAgent?.name ?? null;
  const sourceAgentBaseName = sourceAgentName?.replace(/ \(fork [^)]+\)$/, "") ?? null;

  // Friendly label for the source's agent — the "same as source" option shows
  // this so the user sees the actual agent they're keeping. The source's YAML
  // name (e.g. "claude-native-ui") maps to a display name (e.g. "Claude Code")
  // via the built-in catalog; fall back to the raw name while it loads.
  const sourceAgentDisplay =
    (agents ?? []).find(
      (a) =>
        a.id === sourceAgent?.id || a.name === sourceAgentName || a.name === sourceAgentBaseName,
    )?.display_name ??
    sourceAgentBaseName ??
    sourceAgentName ??
    "the original agent";

  // Switch targets, excluding:
  //   1. the source's OWN agent — "Same as source" already represents
  //      keeping it, so listing it again is a confusing duplicate (e.g.
  //      a Claude Code session showing both "Same as source" and
  //      "Claude Code"). Matched by id and by name (including the
  //      fork-suffix-stripped name), since a UI session may bind the
  //      built-in directly, a same-named clone, or a "(fork …)" clone.
  //   2. targets that wouldn't preserve history: SDK targets replay the
  //      Omnigent transcript and native targets rebuild their on-disk
  //      transcript from the copied items (any source) — see
  //      forkTargetCarriesHistory. Unclassifiable harnesses
  //      (harness=null) are hidden.
  const switchableAgents = (agents ?? []).filter(
    (a) =>
      a.id !== sourceAgent?.id &&
      a.name !== sourceAgentName &&
      a.name !== sourceAgentBaseName &&
      forkTargetCarriesHistory(a.harness),
  );

  const switching = agentChoice !== SAME_AS_SOURCE;

  // Default the host = source host (when online) else the first online
  // host, once hosts have loaded. Only fills an empty slot so an explicit
  // pick is never overridden.
  useEffect(() => {
    if (!isCodingSource || selectedHostId !== null) return;
    if (sourceHostId && sourceHostOnline) {
      setSelectedHostId(sourceHostId);
    } else if (onlineHosts.length > 0) {
      setSelectedHostId(onlineHosts[0].host_id);
    }
  }, [isCodingSource, selectedHostId, sourceHostId, sourceHostOnline, onlineHosts]);

  // Prefill the directory with the source's workspace — but only when staying
  // on the source host. On a different host that path is a different machine,
  // so leave it blank for the user to pick.
  useEffect(() => {
    if (onSourceHost && workspace === "" && sourceWorkspace) {
      setWorkspace(sourceWorkspace);
    }
  }, [onSourceHost, workspace, sourceWorkspace]);

  // When the source used a worktree, default the base ref to that branch so
  // the clone branches off where the original left work — again only on the
  // source host, where that branch exists.
  useEffect(() => {
    if (onSourceHost && baseBranch === "" && sourceGitBranch) {
      setBaseBranch(sourceGitBranch);
    }
  }, [onSourceHost, baseBranch, sourceGitBranch]);

  const workspaceTrimmed = normalizeWorkspacePath(workspace) ?? "";
  const workspaceValid = isValidWorkspace(workspace);
  // The picked host must still be ONLINE, not merely selected: hosts refetch
  // periodically, so a previously-picked host can go offline while selected.
  // Gating on online-ness keeps the button greyed (and avoids a launchRunner
  // that would just fail server-side) until a live host is chosen.
  const selectedHostOnline =
    selectedHostId !== null && onlineHosts.some((h) => h.host_id === selectedHostId);
  // A coding source can only start once a live host + valid directory are picked.
  const canSubmit = !isCodingSource || (selectedHostOnline && workspaceValid);

  // Conflict hint: other *connected* sessions already working in the picked
  // directory on this host (same wiring as NewChatDialog).
  const { data: directorySessions } = useDirectorySessions(
    isCodingSource && Boolean(selectedHostId),
  );
  const conflictCandidates = useMemo(
    () =>
      isCodingSource
        ? (directorySessions ?? []).filter(
            (s) => s.host_id === selectedHostId && s.workspace != null,
          )
        : [],
    [isCodingSource, directorySessions, selectedHostId],
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

  // Reveal Advanced (once) only when running on a DIFFERENT host than the
  // source — a fresh directory must be picked there, so the field can't stay
  // hidden. On the source host it stays collapsed (the defaults need no
  // input); a directory conflict is surfaced inline at the top instead of
  // force-opening Advanced, since cloning a *running* session always trips it
  // (the original is still in that directory).
  useEffect(() => {
    if (onDifferentHost && !autoExpandedRef.current) {
      autoExpandedRef.current = true;
      setShowAdvanced(true);
    }
  }, [onDifferentHost]);

  // Mismatched-directory warning: the transcript's file references were
  // grounded in the source's directory ON the source's host. A different
  // directory — or a different host, where even an identical path is a
  // different machine — won't resolve them, so the agent must re-orient.
  const sourceWorkspaceNorm = sourceWorkspace ? normalizeWorkspacePath(sourceWorkspace) : null;
  const hostMismatch =
    sourceHostId != null && selectedHostId !== null && selectedHostId !== sourceHostId;
  const showMismatchWarning =
    isCodingSource &&
    ((hostMismatch && workspaceTrimmed !== "") ||
      (sourceWorkspaceNorm !== null &&
        workspaceTrimmed !== "" &&
        workspaceTrimmed !== sourceWorkspaceNorm));

  // Default state: a coding clone on the source host still pointed at the
  // source's directory. Drives the "reuses the original's working directory"
  // indicator, which explains the default without forcing Advanced open. On a
  // different host this is false (the mismatch warning takes over instead).
  const usingSourceDir = onSourceHost && workspaceTrimmed !== "" && !showMismatchWarning;

  function commitWorkspacePath(path: string): void {
    setWorkspace(path);
    setBrowsing(true);
    setBrowseNonce((n) => n + 1);
  }

  async function handleFork(): Promise<void> {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const trimmed = title.trim();
      // Empty title → omit so the server derives "Fork of <source title>".
      const fork = await forkSession(
        sourceSessionId,
        trimmed === "" ? undefined : trimmed,
        switching ? agentChoice : undefined,
        upToResponseId ?? undefined,
      );
      // Coding fork: launch the runner in the BACKGROUND, then navigate
      // into the (already-created, unbound) clone immediately — awaiting the
      // launch would block the modal for a worktree create (up to minutes)
      // and hang on a dropped response. If the launch fails the clone stays
      // unbound; ChatPage's existing unbound-fork path lets the user retry
      // the bind via the directory picker. (A follow-up will surface the
      // failure proactively + show "Connecting…" for the whole launch.)
      if (isCodingSource && selectedHostId) {
        const trimmedBranch = branchName.trim();
        addRecent(workspaceTrimmed);
        void launchRunner(
          selectedHostId,
          fork.id,
          workspaceTrimmed,
          trimmedBranch
            ? { branchName: trimmedBranch, baseBranch: baseBranch.trim() || undefined }
            : undefined,
        ).catch((e) => {
          // Swallow: recovery is the unbound-fork picker on the session
          // page. Logged so a failed launch isn't entirely silent.
          console.warn(`Clone ${fork.id}: background runner launch failed`, e);
        });
      }
      // Fire-and-forget: the sidebar refresh must not gate navigation.
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      onClose();
      navigate(`/c/${fork.id}`);
    } catch (e) {
      // forkSession failed — nothing created, so inputs stay editable for a resubmit.
      setError(e instanceof Error ? e.message : "Couldn't clone the session. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  // Suggested name shown as the input placeholder; blank input → the server
  // applies this same "Fork of <source title>" default.
  const namePlaceholder = defaultForkTitle(sourceTitle) || "Name the cloned session";

  return (
    <>
      <div className="-mr-4 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto pr-4 [scrollbar-width:thin] [&::-webkit-scrollbar]:w-2 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-border [&::-webkit-scrollbar-track]:bg-transparent">
        {/* Host first: with no online host there is nothing to run the
              clone on, so the user learns up front whether they can proceed.
              Mirrors NewChatDialog: a picker when hosts are online (with a
              collapsible "connect another" CLI hint), or the connect
              instructions directly when none are. */}
        {isCodingSource && (
          <div className="flex flex-col gap-2">
            <span className="text-xs font-medium text-muted-foreground">Host</span>
            {hosts === undefined ? (
              <p className="text-xs text-muted-foreground" data-testid="fork-session-no-hosts">
                Loading hosts…
              </p>
            ) : onlineHosts.length === 0 ? (
              // Nothing usable (no hosts, or all offline) — show the connect
              // command directly so the user can unblock. The submit button
              // stays greyed until a host is online.
              <ConnectHostInstructions
                serverUrl={serverUrl}
                label={
                  allHosts.length === 0
                    ? "No hosts connected yet. Connect one from your terminal:"
                    : "No hosts online. Reconnect from your terminal to start the clone:"
                }
              />
            ) : (
              <>
                <Select
                  value={selectedHostId ?? ""}
                  onValueChange={(v) => {
                    setSelectedHostId(v);
                    // Workspace AND the worktree base ref are host-specific:
                    // the directory path and the prefilled source branch only
                    // make sense on the source machine. Clear all three on a
                    // host change so a stale base ref can't launch a worktree
                    // on the new host. (The source-host prefill effects
                    // re-seed them if the user switches back.)
                    setWorkspace("");
                    setBranchName("");
                    setBaseBranch("");
                    setBrowsing(false);
                  }}
                >
                  <SelectTrigger className="w-full text-xs" data-testid="fork-session-host-select">
                    <SelectValue placeholder="Select a host" />
                  </SelectTrigger>
                  <SelectContent>
                    {onlineHosts.map((host) => (
                      <SelectItem
                        key={host.host_id}
                        value={host.host_id}
                        data-testid={`fork-session-host-option-${host.host_id}`}
                      >
                        <HostLabel host={host} />
                      </SelectItem>
                    ))}
                    {offlineHosts.map((host) => (
                      <SelectItem
                        key={host.host_id}
                        value={host.host_id}
                        disabled
                        data-testid={`fork-session-host-option-${host.host_id}`}
                      >
                        <HostLabel host={host} />
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <button
                  type="button"
                  onClick={() => setShowConnect((v) => !v)}
                  className="flex cursor-pointer items-center gap-1 self-start text-xs text-muted-foreground transition hover:text-foreground"
                  data-testid="fork-session-connect-host-toggle"
                >
                  {showConnect ? (
                    <ChevronUpIcon className="size-3.5" />
                  ) : (
                    <ChevronDownIcon className="size-3.5" />
                  )}
                  Connect another host from your terminal
                </button>
                {showConnect && <ConnectHostInstructions serverUrl={serverUrl} />}
              </>
            )}
          </div>
        )}

        <div className="flex flex-col gap-1.5">
          <label htmlFor="fork-session-agent" className="text-xs font-medium text-muted-foreground">
            Agent
          </label>
          <Select value={agentChoice} onValueChange={setAgentChoice}>
            <SelectTrigger
              id="fork-session-agent"
              data-testid="fork-session-agent-select"
              className="w-full text-xs"
            >
              {/* Custom value so the default reads "<agent> (same as original
                    session)" with the parenthetical greyed, mirroring the option. */}
              <SelectValue>
                {switching ? (
                  (switchableAgents.find((a) => a.id === agentChoice)?.display_name ??
                  sourceAgentDisplay)
                ) : (
                  <>
                    {sourceAgentDisplay}{" "}
                    <span className="text-muted-foreground">(same as original session)</span>
                  </>
                )}
              </SelectValue>
            </SelectTrigger>
            <SelectContent position="popper" align="start">
              <SelectItem
                value={SAME_AS_SOURCE}
                data-testid="fork-session-agent-option-same"
                className="text-xs"
              >
                {sourceAgentDisplay}{" "}
                <span className="text-muted-foreground">(same as original session)</span>
              </SelectItem>
              {switchableAgents.map((agent) => (
                <SelectItem
                  key={agent.id}
                  value={agent.id}
                  data-testid={`fork-session-agent-option-${agent.id}`}
                  className="text-xs"
                >
                  {agent.display_name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        {/* Indicator: by default the clone reuses the source's working
              directory; changing it lives under Advanced settings. */}
        {usingSourceDir && (
          <p className="text-xs text-muted-foreground" data-testid="fork-session-reuse-dir-hint">
            By default the clone reuses the original session's{" "}
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  className="cursor-pointer underline decoration-dotted underline-offset-2"
                  data-testid="fork-session-reuse-dir-path"
                >
                  working directory
                </button>
              </TooltipTrigger>
              <TooltipContent className="font-mono break-all">{workspaceTrimmed}</TooltipContent>
            </Tooltip>
            . Open Advanced settings to change it.
          </p>
        )}

        {/* Conflict warning at the top level (not inside Advanced) so it's
              visible without expanding — cloning a running session always
              shares its directory with the still-active original. */}
        {showConflictHint && (
          <p
            className="flex items-start gap-1.5 text-xs text-warning"
            data-testid="fork-session-conflict-hint"
          >
            <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
            <span>
              {conflictingSessions.length === 1
                ? "1 other agent is"
                : `${conflictingSessions.length} other agents are`}{" "}
              working in this directory, so writes may conflict. Name a git branch under Advanced
              settings to work in an isolated copy.
            </span>
          </p>
        )}

        {/* Name and (for coding sources) working directory + git worktree
              live behind Advanced, collapsed by default — everything here
              prefills sensibly, so the common path needs no input. Mirrors
              NewChatDialog's advanced toggle. */}
        <div className="flex flex-col gap-4">
          <button
            type="button"
            onClick={() => setShowAdvanced((v) => !v)}
            className="flex cursor-pointer items-center gap-1 self-start text-xs font-medium text-foreground transition hover:text-foreground"
            data-testid="fork-session-advanced-toggle"
            aria-expanded={showAdvanced}
            aria-controls="fork-session-advanced-content"
          >
            {showAdvanced ? (
              <ChevronUpIcon className="size-3.5" />
            ) : (
              <ChevronDownIcon className="size-3.5" />
            )}
            Advanced settings
          </button>

          {showAdvanced && (
            <div
              id="fork-session-advanced-content"
              className="flex flex-col gap-4"
              data-testid="fork-session-advanced-content"
            >
              <div className="flex flex-col gap-1.5">
                <label
                  htmlFor="fork-session-title"
                  className="text-xs font-medium text-muted-foreground"
                >
                  Name (optional)
                </label>
                <input
                  id="fork-session-title"
                  data-testid="fork-session-title-input"
                  type="text"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !submitting && canSubmit) handleFork();
                  }}
                  placeholder={namePlaceholder}
                  className="rounded-md border border-input bg-background px-3 py-2 font-mono text-xs outline-none transition-colors focus-visible:border-ring"
                />
              </div>

              {isCodingSource && (
                <>
                  <div className="flex flex-col gap-2">
                    <span className="text-xs font-medium text-muted-foreground">
                      Working directory
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
                            initialPath={
                              isNavigablePath(workspaceTrimmed) ? workspaceTrimmed : undefined
                            }
                            onSelect={(path) => {
                              setWorkspace(path);
                              setBrowsing(false);
                            }}
                            onClose={() => setBrowsing(false)}
                          />
                        )}
                        {showMismatchWarning && (
                          <p
                            className="flex items-start gap-1.5 text-xs text-warning"
                            data-testid="fork-session-mismatch-warning"
                          >
                            <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
                            <span>
                              This directory differs from the original session's. Earlier file
                              references in the transcript may not apply — the agent will need to
                              re-orient.
                            </span>
                          </p>
                        )}
                      </>
                    ) : (
                      <p className="text-xs text-muted-foreground">
                        Select a host to choose a directory.
                      </p>
                    )}
                  </div>

                  <div className="flex flex-col gap-1">
                    <label
                      htmlFor="fork-session-branch"
                      className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground"
                    >
                      <GitBranchIcon className="size-3.5" />
                      Git worktree (optional)
                    </label>
                    <input
                      id="fork-session-branch"
                      type="text"
                      value={branchName}
                      onChange={(e) => setBranchName(e.target.value)}
                      placeholder="feature/my-branch"
                      data-testid="fork-session-branch-input"
                      className="rounded-md border border-input bg-background px-3 py-2 font-mono text-xs outline-none transition-colors focus-visible:border-ring"
                    />
                    {branchName.trim() !== "" && (
                      <input
                        id="fork-session-base-branch"
                        type="text"
                        value={baseBranch}
                        onChange={(e) => setBaseBranch(e.target.value)}
                        placeholder="Base branch (defaults to the current branch)"
                        aria-label="Base branch"
                        data-testid="fork-session-base-branch-input"
                        className="rounded-md border border-input bg-background px-3 py-2 font-mono text-xs outline-none transition-colors focus-visible:border-ring"
                      />
                    )}
                    <p className="text-xs text-muted-foreground">
                      Creates a git worktree for a new branch in an isolated directory — keeps the
                      clone from fighting the original over the same files. Leave blank to start in
                      the picked directory.
                    </p>
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      </div>

      {error !== null && (
        <p data-testid="fork-session-error" className="text-xs text-destructive">
          {error}
        </p>
      )}

      <DialogFooter>
        <Button variant="ghost" onClick={onClose} disabled={submitting}>
          Cancel
        </Button>
        <Button
          data-testid="fork-session-submit"
          onClick={handleFork}
          disabled={submitting || !canSubmit}
        >
          {submitting
            ? isCodingSource
              ? "Starting…"
              : "Cloning…"
            : isCodingSource
              ? "Clone & start"
              : "Clone"}
        </Button>
      </DialogFooter>
    </>
  );
}

/**
 * Clone/fork dialog for a session — the header menu's Clone surface.
 * A thin `Dialog` shell (title + info tooltip) around
 * {@link ForkSessionForm}, which holds all the fork logic and state.
 * The form lives inside `DialogContent`, so closing the dialog unmounts
 * and resets it.
 *
 * @param sourceSessionId - Session being forked.
 * @param sourceTitle - Source title, used to prefill the fork's name.
 * @param sourceWorkspace - Source workspace; presence marks a coding source
 *   (shows the host/dir fields) and seeds the directory default.
 * @param sourceHostId - Source host; default host when it is online.
 * @param sourceGitBranch - Source git branch; seeds the worktree base ref.
 * @param upToResponseId - Truncation point for a "fork from here" opened
 *   from a message's actions: the fork copies history only up to and
 *   including this response. `null` / omitted clones the full history.
 * @param open - Whether the dialog is visible.
 * @param onOpenChange - Visibility setter (Radix-controlled).
 */
export function ForkSessionDialog({
  sourceSessionId,
  sourceTitle,
  sourceWorkspace,
  sourceHostId,
  sourceGitBranch,
  upToResponseId,
  open,
  onOpenChange,
}: {
  sourceSessionId: string;
  sourceTitle?: string | null;
  sourceWorkspace?: string | null;
  sourceHostId?: string | null;
  sourceGitBranch?: string | null;
  upToResponseId?: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const truncated = upToResponseId != null;
  // Shown in the title's info tooltip (and a visually-hidden DialogDescription
  // for screen readers). Coding sources also start on a picked host/directory.
  const cloneDescription = `${
    truncated
      ? "Copies this session's history up to the selected response into a new session you own — messages after it aren't carried over"
      : "Copies this session's history into a new session you own"
  }${
    sourceWorkspace ? ", then starts it on the host and directory you pick" : ""
  }. Comments aren't copied, and changes in the clone won't affect the original.`;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        data-testid="fork-session-dialog"
        className="flex max-h-[85vh] flex-col gap-4 sm:max-w-lg"
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-1.5">
            {truncated ? "Fork from this response" : "Clone session"}
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  aria-label="What does cloning do?"
                  data-testid="fork-session-info"
                  // tabIndex=-1 keeps the dialog's open-autofocus (and tabbing)
                  // off this icon, so the tooltip only opens on hover — not the
                  // moment the modal appears. The same text lives in the
                  // sr-only DialogDescription below, so AT users still get it.
                  tabIndex={-1}
                  className="cursor-pointer text-muted-foreground transition-colors hover:text-foreground"
                >
                  <InfoIcon className="size-4" />
                </button>
              </TooltipTrigger>
              <TooltipContent>{cloneDescription}</TooltipContent>
            </Tooltip>
          </DialogTitle>
          {/* Description moved into the title's info tooltip; kept here visually
              hidden so the dialog stays described for screen readers. */}
          <DialogDescription className="sr-only">{cloneDescription}</DialogDescription>
        </DialogHeader>
        <ForkSessionForm
          sourceSessionId={sourceSessionId}
          sourceTitle={sourceTitle}
          sourceWorkspace={sourceWorkspace}
          sourceHostId={sourceHostId}
          sourceGitBranch={sourceGitBranch}
          upToResponseId={upToResponseId}
          onClose={() => onOpenChange(false)}
        />
      </DialogContent>
    </Dialog>
  );
}
