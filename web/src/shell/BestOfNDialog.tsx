import { useEffect, useId, useMemo, useState, type FormEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { PlusIcon, XIcon } from "lucide-react";
import { toast } from "sonner";
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
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import { useBulkArchiveConversations, type Conversation } from "@/hooks/useConversations";
import { useGridSessions } from "@/grid/GridSessionsContext";
import { partitionPickerEntries, selectableSessionAgents } from "@/lib/agentGrouping";
import { readDefaultBaseBranch } from "@/lib/baseBranchPreferences";
import {
  BEST_OF_N_GROUP_LABEL,
  BEST_OF_N_MAX_ATTEMPTS,
  BEST_OF_N_MIN_ATTEMPTS,
  bestOfNRepository,
  launchBestOfN,
  otherBestOfNAttemptIds,
  verifiedOtherAttempts,
  type VerifiedAttempts,
} from "@/lib/bestOfN";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled, pickerCuration, type ServerInfo } from "@/lib/capabilities";
import { randomUUID } from "@/lib/randomUUID";
import { useNavigate } from "@/lib/routing";
import { fetchFirstUserPrompt, getSessionSlim } from "@/lib/sessionsApi";
import { splitWorktreePath } from "@/lib/worktreePaths";

/** Whether a session can start a best-of-N run: an owned, active session on a host workspace. */
export function canStartBestOfN(
  info: ServerInfo | "loading",
  conversation: Pick<Conversation, "archived" | "host_id" | "workspace">,
  isOwner: boolean,
): boolean {
  return (
    isOwner &&
    conversation.archived !== true &&
    info !== "loading" &&
    info.sibling_sessions === true &&
    isFeatureEnabled(info, "best_of_n") &&
    !!conversation.host_id &&
    !!conversation.workspace
  );
}

interface AttemptRow {
  key: string;
  agentId: string;
}

/** The first two distinct harnesses, starting with the source session's agent when pickable. */
function defaultAttemptAgents(agents: readonly AvailableAgent[], sourceAgentId?: string): string[] {
  const { harnesses } = partitionPickerEntries(agents);
  const candidates = harnesses.length >= BEST_OF_N_MIN_ATTEMPTS ? harnesses : agents;
  const first = agents.find((agent) => agent.id === sourceAgentId) ?? candidates[0];
  if (!first) return [];
  const second = candidates.find((agent) => agent.id !== first.id) ?? first;
  return [first.id, second.id];
}

function AttemptAgentSelect({
  index,
  value,
  harnesses,
  agents,
  disabled,
  onChange,
}: {
  index: number;
  value: string;
  harnesses: readonly AvailableAgent[];
  agents: readonly AvailableAgent[];
  disabled: boolean;
  onChange: (agentId: string) => void;
}) {
  const options = (group: readonly AvailableAgent[]) =>
    group.map((agent) => (
      <SelectItem key={agent.id} value={agent.id} className="text-sm">
        {agent.display_name}
      </SelectItem>
    ));
  return (
    <Select
      value={value}
      onValueChange={onChange}
      disabled={disabled}
      componentId="best_of_n.attempt_agent"
    >
      <SelectTrigger
        className="w-full text-sm"
        aria-label={`Attempt ${index + 1} agent`}
        data-testid={`best-of-n-attempt-${index}`}
      >
        <SelectValue placeholder="Choose an agent" />
      </SelectTrigger>
      <SelectContent position="popper" align="start">
        {options(harnesses)}
        {harnesses.length > 0 && agents.length > 0 && <SelectSeparator />}
        {options(agents)}
      </SelectContent>
    </Select>
  );
}

/**
 * Start the same task on two to four agents. Each attempt runs in its own new
 * worktree of the source session's repository, as a nested agent of it, and
 * the attempts open side by side in a session grid.
 */
export function BestOfNDialog({
  conversation,
  open,
  onOpenChange,
}: {
  conversation: Pick<
    Conversation,
    "id" | "title" | "host_id" | "workspace" | "agent_id" | "git_branch"
  >;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const info = useServerInfo();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const grids = useGridSessions();
  const promptId = useId();
  const baseBranchId = useId();
  const { data: availableAgents } = useAvailableAgents({ enabled: open });
  const curation = pickerCuration(info);
  const pickable = useMemo(
    () => selectableSessionAgents(availableAgents ?? [], curation.restrictToAgentNames),
    [availableAgents, curation.restrictToAgentNames],
  );
  const { harnesses, agents } = useMemo(() => partitionPickerEntries(pickable), [pickable]);
  const [prompt, setPrompt] = useState("");
  const [promptTouched, setPromptTouched] = useState(false);
  // Null until the user edits the attempts; until then they follow the defaults.
  const [editedAttempts, setEditedAttempts] = useState<AttemptRow[] | null>(null);
  // A worktree session's own branch, so attempts build on its committed work.
  const [baseBranch, setBaseBranch] = useState(
    () =>
      conversation.git_branch ??
      splitWorktreePath(conversation.workspace ?? "")?.branchDir ??
      readDefaultBaseBranch() ??
      "",
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const repository = bestOfNRepository(conversation.workspace ?? "");

  // Prefill the task with the session's first prompt unless the user has typed.
  useEffect(() => {
    if (!open || promptTouched) return;
    const controller = new AbortController();
    fetchFirstUserPrompt(conversation.id, controller.signal)
      .then((text) => {
        if (text !== null && !controller.signal.aborted) setPrompt((current) => current || text);
      })
      .catch(() => {});
    return () => controller.abort();
  }, [open, promptTouched, conversation.id]);

  const attempts =
    editedAttempts ??
    defaultAttemptAgents(pickable, conversation.agent_id).map((agentId, index) => ({
      key: `default-${index}`,
      agentId,
    }));
  const agentById = useMemo(() => new Map(pickable.map((agent) => [agent.id, agent])), [pickable]);
  const chosenAgents = attempts.flatMap(({ agentId }) => {
    const agent = agentById.get(agentId);
    return agent ? [agent] : [];
  });
  const ready =
    prompt.trim() !== "" &&
    chosenAgents.length === attempts.length &&
    attempts.length >= BEST_OF_N_MIN_ATTEMPTS &&
    !!conversation.host_id &&
    repository !== "";

  const setAttempt = (key: string, agentId: string) =>
    setEditedAttempts(attempts.map((row) => (row.key === key ? { ...row, agentId } : row)));

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!ready || submitting || !conversation.host_id) return;
    setSubmitting(true);
    setError(null);
    try {
      const result = await launchBestOfN({
        sourceSessionId: conversation.id,
        hostId: conversation.host_id,
        repository,
        baseBranch,
        prompt: prompt.trim(),
        agents: chosenAgents,
      });
      if (result.sessionIds.length === 0) {
        setError(result.failures[0]?.message ?? "No attempt could be started.");
        return;
      }
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      for (const failure of result.failures)
        toast.error(`${failure.agentName}: ${failure.message}`);
      onOpenChange(false);
      const name = `Best of ${result.sessionIds.length}`;
      if (
        result.sessionIds.length < BEST_OF_N_MIN_ATTEMPTS ||
        !grids?.openGrid(result.sessionIds, name, result.groupId)
      )
        navigate(`/c/${result.sessionIds[0]}`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "No attempt could be started.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !submitting && onOpenChange(next)}>
      <DialogContent className="sm:max-w-lg" data-testid="best-of-n-dialog">
        <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-4">
          <DialogHeader>
            <DialogTitle>Best of N</DialogTitle>
            <DialogDescription>
              Run one task on {BEST_OF_N_MIN_ATTEMPTS}–{BEST_OF_N_MAX_ATTEMPTS} agents at once. Each
              attempt gets its own worktree and branch from the same base.{" "}
              {grids?.enabled
                ? "They open side by side in a grid so you can keep the best one."
                : "They appear under this session in the sidebar so you can keep the best one."}
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-1.5">
            <label htmlFor={promptId} className="text-sm text-muted-foreground">
              Task
            </label>
            <Textarea
              id={promptId}
              value={prompt}
              onChange={(event) => {
                setPromptTouched(true);
                setPrompt(event.target.value);
              }}
              placeholder="Describe the task every attempt should do"
              rows={4}
              disabled={submitting}
              data-testid="best-of-n-prompt"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <span className="text-sm text-muted-foreground">Attempts</span>
            {attempts.map(({ key, agentId }, index) => (
              <div key={key} className="flex items-center gap-2">
                <AttemptAgentSelect
                  index={index}
                  value={agentId}
                  harnesses={harnesses}
                  agents={agents}
                  disabled={submitting}
                  onChange={(next) => setAttempt(key, next)}
                />
                {attempts.length > BEST_OF_N_MIN_ATTEMPTS && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-xs"
                    aria-label={`Remove attempt ${index + 1}`}
                    disabled={submitting}
                    onClick={() => setEditedAttempts(attempts.filter((row) => row.key !== key))}
                  >
                    <XIcon className="size-3.5" />
                  </Button>
                )}
              </div>
            ))}
            {attempts.length < BEST_OF_N_MAX_ATTEMPTS && attempts.length > 0 && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="self-start"
                disabled={submitting}
                onClick={() =>
                  setEditedAttempts([
                    ...attempts,
                    { key: randomUUID(), agentId: attempts[attempts.length - 1].agentId },
                  ])
                }
                data-testid="best-of-n-add-attempt"
              >
                <PlusIcon className="size-3.5" />
                Add attempt
              </Button>
            )}
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor={baseBranchId} className="text-sm text-muted-foreground">
              Base branch
            </label>
            <Input
              id={baseBranchId}
              value={baseBranch}
              onChange={(event) => setBaseBranch(event.target.value)}
              placeholder="The repository's current branch"
              disabled={submitting}
              data-testid="best-of-n-base-branch"
            />
            <p className="truncate text-xs text-muted-foreground" title={repository}>
              New worktrees of {repository || "this session's workspace"}
            </p>
          </div>
          {error !== null && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              disabled={submitting}
              onClick={() => onOpenChange(false)}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={!ready || submitting}
              data-testid="best-of-n-start"
              componentId="best_of_n.start"
            >
              {submitting ? "Starting…" : `Start ${attempts.length} attempts`}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Confirm keeping one best-of-N attempt: archive the other attempts, and
 * optionally remove their worktrees (their branches stay). Only attempts that
 * verify as owned members of the same run are listed and archived.
 */
export function KeepAttemptDialog({
  sessionId,
  title,
  labels,
  open,
  onOpenChange,
}: {
  sessionId: string;
  title: string;
  /** The kept attempt's labels, naming its run. */
  labels: Record<string, string> | null | undefined;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const info = useServerInfo();
  const navigate = useNavigate();
  const grids = useGridSessions();
  const bulkArchive = useBulkArchiveConversations();
  const checkboxId = useId();
  const cleanupSupported = info !== "loading" && info.archive_worktree_cleanup === true;
  const [removeWorktrees, setRemoveWorktrees] = useState(false);
  const [verified, setVerified] = useState<VerifiedAttempts | null>(null);
  const [checks, setChecks] = useState(0);
  const [archiving, setArchiving] = useState(false);
  const labelKey = JSON.stringify(labels ?? {});

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const keptLabels = JSON.parse(labelKey) as Record<string, string>;
    setVerified(null);
    verifiedOtherAttempts(sessionId, keptLabels, (id) => getSessionSlim(id))
      .then((result) => !cancelled && setVerified(result))
      .catch(
        () =>
          !cancelled &&
          setVerified({
            attempts: [],
            unresolved: otherBestOfNAttemptIds(sessionId, keptLabels),
          }),
      );
    return () => {
      cancelled = true;
    };
  }, [open, sessionId, labelKey, checks]);

  const others = verified?.attempts ?? null;
  const unresolved = verified?.unresolved.length ?? 0;
  const count = others?.length ?? 0;
  const keep = async () => {
    // A partial list would leave unchecked attempts running after "Kept".
    if (others === null || count === 0 || unresolved > 0) return;
    const ids = others.map((attempt) => attempt.id);
    setArchiving(true);
    try {
      await bulkArchive.mutateAsync({
        ids,
        archived: true,
        deleteWorktreeIds: cleanupSupported && removeWorktrees ? new Set(ids) : undefined,
      });
    } catch {
      setArchiving(false);
      toast.error("Some attempts couldn't be archived. Try again from their menus.");
      return;
    }
    onOpenChange(false);
    toast.success(`Kept ${title}. Archived ${count} other attempt${count === 1 ? "" : "s"}.`);
    // Drop the grid this run opened (never one the user built), then open the kept attempt.
    const run = labels?.[BEST_OF_N_GROUP_LABEL];
    if (run) grids?.deleteRunGrids(run);
    navigate(`/c/${sessionId}`);
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !archiving && onOpenChange(next)}>
      <DialogContent className="sm:max-w-md" data-testid="keep-attempt-dialog">
        <DialogHeader>
          <DialogTitle>Keep this attempt?</DialogTitle>
          <DialogDescription>
            {others === null
              ? "Checking the other attempts…"
              : unresolved > 0
                ? "Every other attempt of this run must be checked before keeping this one."
                : count === 0
                  ? "No other attempts of this run can be archived."
                  : `${title} stays. ${count === 1 ? "This attempt is" : `These ${count} attempts are`} archived; you can unarchive ${count === 1 ? "it" : "them"} later.`}
          </DialogDescription>
        </DialogHeader>
        {count > 0 && (
          <ul className="list-disc pl-5 text-sm" data-testid="keep-attempt-others">
            {others!.map((attempt) => (
              <li key={attempt.id}>{attempt.title}</li>
            ))}
          </ul>
        )}
        {unresolved > 0 && (
          <div role="alert" className="flex items-center gap-2 text-sm text-destructive">
            Couldn't check {unresolved === 1 ? "1 other attempt" : `${unresolved} other attempts`}.
            <Button
              type="button"
              variant="ghost"
              size="xs"
              disabled={archiving}
              onClick={() => setChecks((value) => value + 1)}
              data-testid="keep-attempt-recheck"
            >
              Check again
            </Button>
          </div>
        )}
        {cleanupSupported && count > 0 && unresolved === 0 && (
          <div className="flex items-center gap-2">
            <Checkbox
              id={checkboxId}
              checked={removeWorktrees}
              disabled={archiving}
              onCheckedChange={(checked) => setRemoveWorktrees(checked === true)}
              data-testid="keep-attempt-remove-worktrees"
              componentId="best_of_n.keep.remove_worktrees"
            />
            <label htmlFor={checkboxId} className="cursor-pointer text-ui">
              Also delete their worktrees (uncommitted changes are lost; branches are kept)
            </label>
          </div>
        )}
        <DialogFooter>
          <Button
            type="button"
            variant="ghost"
            disabled={archiving}
            onClick={() => onOpenChange(false)}
          >
            Cancel
          </Button>
          <Button
            type="button"
            disabled={count === 0 || unresolved > 0 || archiving}
            onClick={() => void keep()}
            data-testid="keep-attempt-confirm"
            componentId="best_of_n.keep.confirm"
          >
            {archiving ? "Archiving…" : "Keep and archive others"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
