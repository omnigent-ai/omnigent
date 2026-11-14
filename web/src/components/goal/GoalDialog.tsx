import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { CheckIcon, Loader2Icon, PauseCircleIcon, PlayCircleIcon, TargetIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  clearGoal,
  setGoal,
  updateGoalStatus,
  type Goal,
  type GoalStatusUpdate,
} from "@/lib/goalApi";
import { getBasePath } from "@/lib/basePath";
import { getOmnigentHostConfig, getOmnigentServerIdentity } from "@/lib/host";
import { cn } from "@/lib/utils";
import {
  canPauseGoal,
  canResumeGoal,
  formatGoalStatus,
  formatGoalUsage,
  goalModeDraftForGoal,
  isGoalUserMode,
  type GoalModeDraft,
} from "./goalUtils";

type GoalApiScope = string | object;
type GoalMutationAction = "save" | "clear" | GoalStatusUpdate;

interface GoalMutation {
  token: object;
  action: GoalMutationAction;
  settled: boolean;
}

interface GoalMutationListener {
  scope: GoalApiScope;
  key: string;
  notify: () => void;
}

// A request keeps its session ownership across dialog lifetimes.
const goalMutations = new Map<GoalApiScope, Map<string, GoalMutation>>();
const goalMutationListeners = new Set<GoalMutationListener>();
const standaloneGoalApiScope = {};

function getGoalMutation(scope: GoalApiScope, key: string): GoalMutation | null {
  return goalMutations.get(scope)?.get(key) ?? null;
}

function hasGoalMutationOwner(scope: GoalApiScope, key: string): boolean {
  return [...goalMutationListeners].some((owner) => owner.scope === scope && owner.key === key);
}

function publishGoalMutation(scope: GoalApiScope, key: string, mutation: GoalMutation | null) {
  if (mutation) {
    const sessions = goalMutations.get(scope) ?? new Map<string, GoalMutation>();
    sessions.set(key, mutation);
    goalMutations.set(scope, sessions);
  } else {
    const sessions = goalMutations.get(scope);
    sessions?.delete(key);
    if (sessions?.size === 0) goalMutations.delete(scope);
  }
  for (const owner of [...goalMutationListeners]) {
    if (owner.scope === scope && owner.key === key) owner.notify();
  }
}

function subscribeGoalMutation(scope: GoalApiScope, key: string, notify: () => void) {
  const owner = { scope, key, notify };
  goalMutationListeners.add(owner);
  return () => {
    goalMutationListeners.delete(owner);
    const mutation = getGoalMutation(scope, key);
    if (mutation?.settled && !hasGoalMutationOwner(scope, key)) {
      publishGoalMutation(scope, key, null);
    }
  };
}

function startGoalMutation(scope: GoalApiScope, key: string, action: GoalMutationAction) {
  if (getGoalMutation(scope, key)) return null;
  const mutation = { token: {}, action, settled: false };
  publishGoalMutation(scope, key, mutation);
  return mutation;
}

function finishGoalMutation(
  scope: GoalApiScope,
  key: string,
  mutation: GoalMutation,
  reconcile = false,
) {
  if (getGoalMutation(scope, key)?.token !== mutation.token) return;
  publishGoalMutation(
    scope,
    key,
    reconcile && hasGoalMutationOwner(scope, key) ? { ...mutation, settled: true } : null,
  );
}

export interface GoalDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  conversationId: string | null;
  readOnly: boolean;
  goal: Goal | null;
  goalRevision: number;
  onGoalChange: (goal: Goal | null) => void;
  /** Stable for this session connection; a new identity invalidates pending dialog reads. */
  refreshGoal: () => Promise<void>;
}

interface GoalSummaryProps {
  loading: boolean;
  goal: Goal | null;
}

/**
 * Render the current goal state inside the dialog.
 *
 * @param props - Current loading and goal state.
 * @param props.loading - ``true`` until the opening read settles or current state arrives.
 * @param props.goal - Current goal, or ``null`` when no goal is set.
 * @returns Current-goal summary element.
 */
function GoalSummary({ loading, goal }: GoalSummaryProps) {
  if (loading) {
    return (
      <div className="flex items-center gap-2 text-ui text-muted-foreground">
        <Loader2Icon className="size-4 animate-spin" />
        <span>Loading goal</span>
      </div>
    );
  }
  if (!goal) {
    return (
      <p data-testid="goal-empty" className="text-ui text-muted-foreground">
        No goal set.
      </p>
    );
  }
  return (
    <div
      data-testid="goal-current"
      className="space-y-1 rounded-lg border border-border bg-muted/30 p-3"
    >
      <div className="flex items-center justify-between gap-3 text-sm">
        <span className="font-medium text-foreground">{formatGoalStatus(goal.status)}</span>
        <span className="shrink-0 text-muted-foreground">{formatGoalUsage(goal)}</span>
      </div>
      <p className="text-ui leading-5 wrap-anywhere whitespace-pre-wrap">{goal.objective}</p>
    </div>
  );
}

interface GoalEditorProps {
  objective: string;
  tokenBudget: string;
  modeDraft: GoalModeDraft;
  goal: Goal | null;
  readOnly: boolean;
  busy: boolean;
  error: string | null;
  onObjectiveChange: (value: string) => void;
  onTokenBudgetChange: (value: string) => void;
  onModeChange: (value: GoalModeDraft) => void;
}

/**
 * Render editable goal fields.
 *
 * @param props - Field values, disabled state, and change handlers.
 * @param props.objective - Draft goal objective text.
 * @param props.tokenBudget - Draft token budget text.
 * @param props.modeDraft - Draft user-selected goal mode.
 * @param props.goal - Current goal, used to preserve non-user-owned statuses.
 * @param props.readOnly - ``true`` when the user lacks edit permission.
 * @param props.busy - ``true`` while a goal operation is in flight.
 * @param props.error - Current validation or API error, if any.
 * @param props.onObjectiveChange - Called with updated objective text.
 * @param props.onTokenBudgetChange - Called with updated budget text.
 * @param props.onModeChange - Called with updated mode draft.
 * @returns Editor fields for the dialog.
 */
function GoalEditor({
  objective,
  tokenBudget,
  modeDraft,
  goal,
  readOnly,
  busy,
  error,
  onObjectiveChange,
  onTokenBudgetChange,
  onModeChange,
}: GoalEditorProps) {
  const showKeepCurrentMode = goal != null && !isGoalUserMode(goal.status);
  const modeButtonClass = (selected: boolean) =>
    cn(
      "inline-flex min-h-9 flex-1 items-center justify-center gap-2 rounded-md px-3 text-ui font-medium transition-colors",
      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
      selected
        ? "bg-background text-foreground shadow-sm"
        : "text-muted-foreground hover:bg-background/60 hover:text-foreground",
      (readOnly || busy) && "pointer-events-none opacity-50",
    );
  return (
    <>
      <div className="space-y-1.5">
        <label className="text-sm font-medium text-muted-foreground" htmlFor="goal-objective">
          Objective
        </label>
        <Textarea
          id="goal-objective"
          value={objective}
          onChange={(event) => onObjectiveChange(event.currentTarget.value)}
          disabled={readOnly || busy}
          maxLength={4000}
          className="min-h-28 resize-y"
          data-testid="goal-objective"
        />
      </div>

      <div className="space-y-1.5">
        <label className="text-sm font-medium text-muted-foreground">Mode</label>
        <div
          role="radiogroup"
          aria-label="Goal mode"
          className="flex w-full gap-1 rounded-lg border border-border bg-muted/30 p-1"
          data-testid="goal-mode"
        >
          {showKeepCurrentMode && (
            <button
              type="button"
              role="radio"
              aria-checked={modeDraft === "keep"}
              className={modeButtonClass(modeDraft === "keep")}
              disabled={readOnly || busy}
              onClick={() => onModeChange("keep")}
              data-testid="goal-mode-keep"
            >
              <CheckIcon className="size-3.5" />
              <span>Keep current</span>
            </button>
          )}
          <button
            type="button"
            role="radio"
            aria-checked={modeDraft === "active"}
            className={modeButtonClass(modeDraft === "active")}
            disabled={readOnly || busy}
            onClick={() => onModeChange("active")}
            data-testid="goal-mode-active"
          >
            <PlayCircleIcon className="size-3.5" />
            <span>Active</span>
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={modeDraft === "paused"}
            className={modeButtonClass(modeDraft === "paused")}
            disabled={readOnly || busy}
            onClick={() => onModeChange("paused")}
            data-testid="goal-mode-paused"
          >
            <PauseCircleIcon className="size-3.5" />
            <span>Paused</span>
          </button>
        </div>
      </div>

      <div className="space-y-1.5">
        <label className="text-sm font-medium text-muted-foreground" htmlFor="goal-token-budget">
          Token budget
        </label>
        <Input
          id="goal-token-budget"
          type="number"
          inputMode="numeric"
          min={1}
          step={1}
          value={tokenBudget}
          onChange={(event) => onTokenBudgetChange(event.currentTarget.value)}
          disabled={readOnly || busy}
          placeholder="Optional"
          data-testid="goal-token-budget"
        />
      </div>

      {error && <p className="text-ui text-destructive">{error}</p>}
    </>
  );
}

interface GoalActionsProps {
  readOnly: boolean;
  busy: boolean;
  saving: boolean;
  clearing: boolean;
  statusUpdating: GoalStatusUpdate | null;
  hasGoal: boolean;
  goal: Goal | null;
  onSave: () => void;
  onClear: () => void;
  onPause: () => void;
  onResume: () => void;
}

interface GoalDialogState {
  objective: string;
  tokenBudget: string;
  modeDraft: GoalModeDraft;
  loading: boolean;
  saving: boolean;
  clearing: boolean;
  statusUpdating: GoalStatusUpdate | null;
  error: string | null;
  setObjectiveDraft: (value: string) => void;
  setTokenBudgetDraft: (value: string) => void;
  setModeDraft: (value: GoalModeDraft) => void;
  saveGoal: () => Promise<void>;
  clearGoal: () => Promise<void>;
  pauseGoal: () => Promise<void>;
  resumeGoal: () => Promise<void>;
}

/**
 * Build a user-facing goal error message.
 *
 * @param prefix - Operation label, e.g. ``"Could not read goal"``.
 * @param err - Thrown value from the API call.
 * @returns Error text for the dialog.
 */
function goalError(prefix: string, err: unknown): string {
  const message = err instanceof Error ? err.message : String(err);
  return `${prefix}: ${message}`;
}

/**
 * Parse a token-budget text field.
 *
 * @param value - Raw input value, e.g. ``"40000"``.
 * @returns Positive integer budget, or ``null`` when the field is blank.
 * @throws Error when the value is not a positive whole number.
 */
export function parseGoalBudget(value: string): number | null {
  const trimmedBudget = value.trim();
  if (!trimmedBudget) return null;
  const parsed = Number(trimmedBudget);
  if (!Number.isSafeInteger(parsed) || parsed <= 0) {
    throw new Error("Token budget must be a positive whole number.");
  }
  return parsed;
}

/**
 * Render goal dialog action buttons.
 *
 * @param props - Button state and callbacks.
 * @param props.readOnly - ``true`` when write actions must be disabled.
 * @param props.busy - ``true`` while any goal operation is running.
 * @param props.saving - ``true`` while set/update is running.
 * @param props.clearing - ``true`` while clear is running.
 * @param props.statusUpdating - Target status while Pause/Resume is running.
 * @param props.hasGoal - ``true`` when a current goal exists.
 * @param props.goal - Current goal, or ``null`` when no status action applies.
 * @param props.onSave - Called to set or update the goal.
 * @param props.onClear - Called to clear the goal.
 * @param props.onPause - Called to pause an active goal.
 * @param props.onResume - Called to resume a paused/blocked/limited goal.
 * @returns Dialog footer actions.
 */
function GoalActions({
  readOnly,
  busy,
  saving,
  clearing,
  statusUpdating,
  hasGoal,
  goal,
  onSave,
  onClear,
  onPause,
  onResume,
}: GoalActionsProps) {
  const showPause = canPauseGoal(goal);
  const showResume = canResumeGoal(goal);
  return (
    <DialogFooter>
      <Button
        type="button"
        variant="outline"
        onClick={onClear}
        disabled={readOnly || busy || !hasGoal}
        loading={clearing}
        data-testid="goal-clear"
      >
        Delete goal
      </Button>
      {showPause && (
        <Button
          type="button"
          variant="outline"
          onClick={onPause}
          disabled={readOnly || busy}
          loading={statusUpdating === "paused"}
          data-testid="goal-pause"
        >
          <PauseCircleIcon className="size-3.5" />
          Pause
        </Button>
      )}
      {showResume && (
        <Button
          type="button"
          variant="outline"
          onClick={onResume}
          disabled={readOnly || busy}
          loading={statusUpdating === "active"}
          data-testid="goal-resume"
        >
          <PlayCircleIcon className="size-3.5" />
          Resume
        </Button>
      )}
      <Button
        type="button"
        onClick={onSave}
        disabled={readOnly || busy}
        loading={saving}
        data-testid="goal-save"
      >
        {hasGoal ? "Update goal" : "Set goal"}
      </Button>
    </DialogFooter>
  );
}

/**
 * Own goal dialog state and API operations.
 *
 * @param props - Session and goal bindings from ``GoalDialog``.
 * @returns Dialog state, field setters, and async goal operations.
 */
function useGoalDialogState({
  open,
  conversationId,
  goal,
  goalRevision,
  onGoalChange,
  refreshGoal,
}: Pick<
  GoalDialogProps,
  "open" | "conversationId" | "goal" | "goalRevision" | "onGoalChange" | "refreshGoal"
>): GoalDialogState {
  const apiScope =
    getOmnigentServerIdentity() ?? getOmnigentHostConfig().fetcher ?? standaloneGoalApiScope;
  const mutationKey = JSON.stringify(["codex_goal", getBasePath(), conversationId]);
  const subscribe = useCallback(
    (notify: () => void) =>
      open && conversationId ? subscribeGoalMutation(apiScope, mutationKey, notify) : () => {},
    [apiScope, conversationId, mutationKey, open],
  );
  const activeMutation = useSyncExternalStore(
    subscribe,
    () => getGoalMutation(apiScope, mutationKey),
    () => null,
  );
  const saving = activeMutation?.action === "save";
  const clearing = activeMutation?.action === "clear";
  const statusUpdating =
    activeMutation?.action === "active" || activeMutation?.action === "paused"
      ? activeMutation.action
      : null;
  const [objective, setObjective] = useState(goal?.objective ?? "");
  const [tokenBudget, setTokenBudget] = useState(goal?.tokenBudget?.toString() ?? "");
  const [modeDraft, setModeDraftState] = useState<GoalModeDraft>(goalModeDraftForGoal(goal));
  const [loading, setLoading] = useState(false);
  const [readError, setReadError] = useState<string | null>(null);
  const [operationError, setOperationError] = useState<string | null>(null);
  const draftOwner = useRef<{ apiScope: GoalApiScope; mutationKey: string } | undefined>(undefined);
  const dirtyFields = useRef(new Set<"objective" | "tokenBudget" | "mode">());
  const dialogBinding = useRef<object | null>(null);
  const currentGoalOwner = useRef({ apiScope, mutationKey, refreshGoal });

  useLayoutEffect(() => {
    currentGoalOwner.current = { apiScope, mutationKey, refreshGoal };
  }, [apiScope, mutationKey, refreshGoal]);

  useLayoutEffect(() => {
    // New dialog or goal-owner bindings invalidate async responses.
    dialogBinding.current = open && conversationId ? {} : null;
    setLoading(false);
    setReadError(null);
    return () => {
      dialogBinding.current = null;
    };
  }, [apiScope, conversationId, mutationKey, open, refreshGoal]);

  useLayoutEffect(() => {
    setOperationError(null);
  }, [apiScope, conversationId, mutationKey, open]);

  useEffect(() => {
    setReadError(null);
    setLoading(false);
  }, [goalRevision]);

  useEffect(() => {
    if (!open) {
      draftOwner.current = undefined;
      return;
    }
    if (
      draftOwner.current?.apiScope !== apiScope ||
      draftOwner.current?.mutationKey !== mutationKey
    ) {
      draftOwner.current = { apiScope, mutationKey };
      dirtyFields.current.clear();
    }
    if (!dirtyFields.current.has("objective")) setObjective(goal?.objective ?? "");
    if (!dirtyFields.current.has("tokenBudget")) {
      setTokenBudget(goal?.tokenBudget?.toString() ?? "");
    }
    if (!dirtyFields.current.has("mode")) setModeDraftState(goalModeDraftForGoal(goal));
  }, [apiScope, goal, mutationKey, open]);

  useEffect(() => {
    const binding = dialogBinding.current;
    if (!binding || !conversationId) return;
    let cancelled = false;
    const refresh = async () => {
      setLoading(true);
      setReadError(null);
      try {
        await refreshGoal();
      } catch (err) {
        if (!cancelled && dialogBinding.current === binding) {
          setReadError(goalError("Could not read goal", err));
        }
      } finally {
        if (!cancelled && dialogBinding.current === binding) setLoading(false);
      }
    };
    void refresh();
    return () => {
      cancelled = true;
    };
  }, [apiScope, conversationId, mutationKey, refreshGoal, open]);

  useEffect(() => {
    const binding = dialogBinding.current;
    if (!binding || !activeMutation?.settled) return;
    let cancelled = false;
    const reconcile = async () => {
      setLoading(true);
      setReadError(null);
      try {
        await refreshGoal();
      } catch (err) {
        if (!cancelled && dialogBinding.current === binding) {
          setReadError(goalError("Could not read goal", err));
        }
      } finally {
        if (!cancelled && dialogBinding.current === binding) {
          setLoading(false);
          finishGoalMutation(apiScope, mutationKey, activeMutation);
        }
      }
    };
    void reconcile();
    return () => {
      cancelled = true;
    };
  }, [activeMutation, apiScope, mutationKey, conversationId, open, refreshGoal]);

  const canApplyMutationResult = (binding: object) => {
    if (dialogBinding.current === binding) return true;
    const current = currentGoalOwner.current;
    // Keep a closed dialog's matching snapshot owner current.
    if (
      current.apiScope === apiScope &&
      current.mutationKey === mutationKey &&
      !hasGoalMutationOwner(apiScope, mutationKey)
    ) {
      void current.refreshGoal().catch(() => {});
    }
    return false;
  };

  const setObjectiveDraft = (value: string) => {
    dirtyFields.current.add("objective");
    setObjective(value);
    setReadError(null);
    setOperationError(null);
  };
  const setTokenBudgetDraft = (value: string) => {
    dirtyFields.current.add("tokenBudget");
    setTokenBudget(value);
    setReadError(null);
    setOperationError(null);
  };
  const setModeDraft = (value: GoalModeDraft) => {
    dirtyFields.current.add("mode");
    setModeDraftState(value);
    setReadError(null);
    setOperationError(null);
  };

  const saveGoal = async () => {
    const binding = dialogBinding.current;
    if (!conversationId || !binding || getGoalMutation(apiScope, mutationKey)) return;
    const trimmedObjective = objective.trim();
    if (!trimmedObjective) {
      setOperationError("Goal objective cannot be empty.");
      return;
    }
    let parsedBudget: number | null;
    try {
      parsedBudget = parseGoalBudget(tokenBudget);
    } catch (err) {
      setOperationError(err instanceof Error ? err.message : String(err));
      return;
    }
    const mutation = startGoalMutation(apiScope, mutationKey, "save");
    if (!mutation) return;
    setReadError(null);
    setOperationError(null);
    try {
      const response = await setGoal(conversationId, {
        objective: trimmedObjective,
        tokenBudget: parsedBudget,
        status: modeDraft === "keep" ? undefined : modeDraft,
      });
      if (!canApplyMutationResult(binding)) return;
      dirtyFields.current.clear();
      onGoalChange(response.goal);
      setObjective(response.goal?.objective ?? trimmedObjective);
      setTokenBudget(response.goal?.tokenBudget?.toString() ?? tokenBudget.trim());
      setModeDraftState(goalModeDraftForGoal(response.goal));
    } catch (err) {
      if (dialogBinding.current === binding)
        setOperationError(goalError("Could not set goal", err));
    } finally {
      finishGoalMutation(apiScope, mutationKey, mutation, dialogBinding.current !== binding);
    }
  };

  const clearCurrentGoal = async () => {
    const binding = dialogBinding.current;
    if (!conversationId || !binding || getGoalMutation(apiScope, mutationKey)) return;
    const mutation = startGoalMutation(apiScope, mutationKey, "clear");
    if (!mutation) return;
    setReadError(null);
    setOperationError(null);
    try {
      await clearGoal(conversationId);
      if (!canApplyMutationResult(binding)) return;
      dirtyFields.current.clear();
      onGoalChange(null);
      setObjective("");
      setTokenBudget("");
      setModeDraftState("active");
    } catch (err) {
      if (dialogBinding.current === binding)
        setOperationError(goalError("Could not clear goal", err));
    } finally {
      finishGoalMutation(apiScope, mutationKey, mutation, dialogBinding.current !== binding);
    }
  };

  const updateStatus = async (status: GoalStatusUpdate) => {
    const binding = dialogBinding.current;
    if (!conversationId || !binding || getGoalMutation(apiScope, mutationKey)) return;
    const mutation = startGoalMutation(apiScope, mutationKey, status);
    if (!mutation) return;
    setReadError(null);
    setOperationError(null);
    try {
      const response = await updateGoalStatus(conversationId, status);
      if (!canApplyMutationResult(binding)) return;
      dirtyFields.current.clear();
      onGoalChange(response.goal);
      setObjective(response.goal?.objective ?? "");
      setTokenBudget(response.goal?.tokenBudget?.toString() ?? "");
      setModeDraftState(goalModeDraftForGoal(response.goal));
    } catch (err) {
      const action = status === "paused" ? "pause" : "resume";
      if (dialogBinding.current === binding)
        setOperationError(goalError(`Could not ${action} goal`, err));
    } finally {
      finishGoalMutation(apiScope, mutationKey, mutation, dialogBinding.current !== binding);
    }
  };

  return {
    objective,
    tokenBudget,
    modeDraft,
    loading,
    saving,
    clearing,
    statusUpdating,
    error: operationError ?? readError,
    setObjectiveDraft,
    setTokenBudgetDraft,
    setModeDraft,
    saveGoal,
    clearGoal: clearCurrentGoal,
    pauseGoal: () => updateStatus("paused"),
    resumeGoal: () => updateStatus("active"),
  };
}

/**
 * Dialog for viewing, setting, and clearing a session goal.
 *
 * @param props - Dialog state and session goal bindings.
 * @param props.open - Whether the dialog is open.
 * @param props.onOpenChange - Called when the dialog open state changes.
 * @param props.conversationId - Active session id, e.g. ``"conv_abc123"``.
 * @param props.readOnly - ``true`` when write actions must be disabled.
 * @param props.goal - Current goal state, or ``null`` when none is set.
 * @param props.goalRevision - Local revision of the latest accepted goal snapshot.
 * @param props.onGoalChange - Called after successful set, clear, or status updates.
 * @param props.refreshGoal - Refresh through the owner of the current goal snapshot.
 * @returns Goal dialog.
 */
export function GoalDialog({
  open,
  onOpenChange,
  conversationId,
  readOnly,
  goal,
  goalRevision,
  onGoalChange,
  refreshGoal,
}: GoalDialogProps) {
  const state = useGoalDialogState({
    open,
    conversationId,
    goal,
    goalRevision,
    onGoalChange,
    refreshGoal,
  });
  const { loading, saving, clearing, statusUpdating } = state;
  const busy = loading || saving || clearing || statusUpdating !== null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="sm:max-w-lg max-h-[85dvh] grid-rows-[auto_1fr_auto]"
        aria-describedby={undefined}
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <TargetIcon className="size-4" />
            <span>Goal</span>
          </DialogTitle>
        </DialogHeader>

        <div className="-m-1 min-h-0 space-y-3 overflow-y-auto p-1">
          <GoalSummary loading={loading} goal={goal} />
          <GoalEditor
            objective={state.objective}
            tokenBudget={state.tokenBudget}
            modeDraft={state.modeDraft}
            goal={goal}
            readOnly={readOnly}
            busy={busy}
            error={state.error}
            onObjectiveChange={state.setObjectiveDraft}
            onTokenBudgetChange={state.setTokenBudgetDraft}
            onModeChange={state.setModeDraft}
          />
        </div>

        <GoalActions
          readOnly={readOnly}
          busy={busy}
          saving={saving}
          clearing={clearing}
          statusUpdating={statusUpdating}
          hasGoal={goal != null}
          goal={goal}
          onSave={() => void state.saveGoal()}
          onClear={() => void state.clearGoal()}
          onPause={() => void state.pauseGoal()}
          onResume={() => void state.resumeGoal()}
        />
      </DialogContent>
    </Dialog>
  );
}
