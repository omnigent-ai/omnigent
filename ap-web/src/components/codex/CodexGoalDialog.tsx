import { useCallback, useEffect, useState } from "react";
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
  clearCodexGoal,
  getCodexGoal,
  setCodexGoal,
  updateCodexGoalStatus,
  type CodexGoal,
  type CodexGoalStatusUpdate,
} from "@/lib/codexGoalApi";
import { cn } from "@/lib/utils";
import {
  canPauseCodexGoal,
  canResumeCodexGoal,
  codexGoalModeDraftForGoal,
  formatCodexGoalStatus,
  formatCodexGoalUsage,
  isCodexGoalUserMode,
  type CodexGoalModeDraft,
} from "./codexGoalUtils";

export interface CodexGoalDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  conversationId: string | null;
  readOnly: boolean;
  goal: CodexGoal | null;
  onGoalChange: (goal: CodexGoal | null) => void;
}

interface CodexGoalSummaryProps {
  loading: boolean;
  goal: CodexGoal | null;
}

/**
 * Render the current Codex goal state inside the dialog.
 *
 * @param props - Current loading and goal state.
 * @param props.loading - ``true`` while the goal GET request is in flight.
 * @param props.goal - Current goal, or ``null`` when no goal is set.
 * @returns Current-goal summary element.
 */
function CodexGoalSummary({ loading, goal }: CodexGoalSummaryProps) {
  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2Icon className="size-4 animate-spin" />
        <span>Loading goal</span>
      </div>
    );
  }
  if (!goal) {
    return (
      <p data-testid="codex-goal-empty" className="text-sm text-muted-foreground">
        No goal set.
      </p>
    );
  }
  return (
    <div
      data-testid="codex-goal-current"
      className="space-y-1 rounded-lg border border-border bg-muted/30 p-3"
    >
      <div className="flex items-center justify-between gap-3 text-xs">
        <span className="font-medium text-foreground">{formatCodexGoalStatus(goal.status)}</span>
        <span className="shrink-0 text-muted-foreground">{formatCodexGoalUsage(goal)}</span>
      </div>
      <p className="text-sm leading-5 whitespace-pre-wrap">{goal.objective}</p>
    </div>
  );
}

interface CodexGoalEditorProps {
  objective: string;
  tokenBudget: string;
  modeDraft: CodexGoalModeDraft;
  goal: CodexGoal | null;
  readOnly: boolean;
  busy: boolean;
  error: string | null;
  onObjectiveChange: (value: string) => void;
  onTokenBudgetChange: (value: string) => void;
  onModeChange: (value: CodexGoalModeDraft) => void;
}

/**
 * Render editable Codex goal fields.
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
function CodexGoalEditor({
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
}: CodexGoalEditorProps) {
  const showKeepCurrentMode = goal != null && !isCodexGoalUserMode(goal.status);
  const modeButtonClass = (selected: boolean) =>
    cn(
      "inline-flex min-h-9 flex-1 items-center justify-center gap-2 rounded-md px-3 text-sm font-medium transition-colors",
      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
      selected
        ? "bg-background text-foreground shadow-sm"
        : "text-muted-foreground hover:bg-background/60 hover:text-foreground",
      (readOnly || busy) && "pointer-events-none opacity-50",
    );
  return (
    <>
      <div className="space-y-1.5">
        <label className="text-xs font-medium text-muted-foreground" htmlFor="codex-goal">
          Objective
        </label>
        <Textarea
          id="codex-goal"
          value={objective}
          onChange={(event) => onObjectiveChange(event.currentTarget.value)}
          disabled={readOnly || busy}
          maxLength={4000}
          className="min-h-28 resize-y"
          data-testid="codex-goal-objective"
        />
      </div>

      <div className="space-y-1.5">
        <label className="text-xs font-medium text-muted-foreground">Mode</label>
        <div
          role="radiogroup"
          aria-label="Goal mode"
          className="flex w-full gap-1 rounded-lg border border-border bg-muted/30 p-1"
          data-testid="codex-goal-mode"
        >
          {showKeepCurrentMode && (
            <button
              type="button"
              role="radio"
              aria-checked={modeDraft === "keep"}
              className={modeButtonClass(modeDraft === "keep")}
              disabled={readOnly || busy}
              onClick={() => onModeChange("keep")}
              data-testid="codex-goal-mode-keep"
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
            data-testid="codex-goal-mode-active"
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
            data-testid="codex-goal-mode-paused"
          >
            <PauseCircleIcon className="size-3.5" />
            <span>Paused</span>
          </button>
        </div>
      </div>

      <div className="space-y-1.5">
        <label
          className="text-xs font-medium text-muted-foreground"
          htmlFor="codex-goal-token-budget"
        >
          Token budget
        </label>
        <Input
          id="codex-goal-token-budget"
          type="number"
          inputMode="numeric"
          min={1}
          step={1}
          value={tokenBudget}
          onChange={(event) => onTokenBudgetChange(event.currentTarget.value)}
          disabled={readOnly || busy}
          placeholder="Optional"
          data-testid="codex-goal-token-budget"
        />
      </div>

      {error && <p className="text-sm text-destructive">{error}</p>}
    </>
  );
}

interface CodexGoalActionsProps {
  readOnly: boolean;
  busy: boolean;
  saving: boolean;
  clearing: boolean;
  statusUpdating: CodexGoalStatusUpdate | null;
  hasGoal: boolean;
  goal: CodexGoal | null;
  onSave: () => void;
  onClear: () => void;
  onPause: () => void;
  onResume: () => void;
}

interface CodexGoalDialogState {
  objective: string;
  tokenBudget: string;
  modeDraft: CodexGoalModeDraft;
  loading: boolean;
  saving: boolean;
  clearing: boolean;
  statusUpdating: CodexGoalStatusUpdate | null;
  error: string | null;
  setObjectiveDraft: (value: string) => void;
  setTokenBudgetDraft: (value: string) => void;
  setModeDraft: (value: CodexGoalModeDraft) => void;
  saveGoal: () => Promise<void>;
  clearGoal: () => Promise<void>;
  pauseGoal: () => Promise<void>;
  resumeGoal: () => Promise<void>;
}

/**
 * Build a user-facing Codex goal error message.
 *
 * @param prefix - Operation label, e.g. ``"Could not read goal"``.
 * @param err - Thrown value from the API call.
 * @returns Error text for the dialog.
 */
function codexGoalError(prefix: string, err: unknown): string {
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
function parseCodexGoalBudget(value: string): number | null {
  const trimmedBudget = value.trim();
  if (!trimmedBudget) return null;
  const parsed = Number(trimmedBudget);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error("Token budget must be a positive whole number.");
  }
  return parsed;
}

/**
 * Render Codex goal dialog action buttons.
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
function CodexGoalActions({
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
}: CodexGoalActionsProps) {
  const showPause = canPauseCodexGoal(goal);
  const showResume = canResumeCodexGoal(goal);
  return (
    <DialogFooter>
      <Button
        type="button"
        variant="outline"
        onClick={onClear}
        disabled={readOnly || busy || !hasGoal}
        data-testid="codex-goal-clear"
      >
        {clearing ? <Loader2Icon className="size-3.5 animate-spin" /> : null}
        Clear
      </Button>
      {showPause && (
        <Button
          type="button"
          variant="outline"
          onClick={onPause}
          disabled={readOnly || busy}
          data-testid="codex-goal-pause"
        >
          {statusUpdating === "paused" ? (
            <Loader2Icon className="size-3.5 animate-spin" />
          ) : (
            <PauseCircleIcon className="size-3.5" />
          )}
          Pause
        </Button>
      )}
      {showResume && (
        <Button
          type="button"
          variant="outline"
          onClick={onResume}
          disabled={readOnly || busy}
          data-testid="codex-goal-resume"
        >
          {statusUpdating === "active" ? (
            <Loader2Icon className="size-3.5 animate-spin" />
          ) : (
            <PlayCircleIcon className="size-3.5" />
          )}
          Resume
        </Button>
      )}
      <Button
        type="button"
        onClick={onSave}
        disabled={readOnly || busy}
        data-testid="codex-goal-save"
      >
        {saving ? <Loader2Icon className="size-3.5 animate-spin" /> : null}
        {hasGoal ? "Update goal" : "Set goal"}
      </Button>
    </DialogFooter>
  );
}

/**
 * Own Codex goal dialog state and API operations.
 *
 * @param props - Session and goal bindings from ``CodexGoalDialog``.
 * @returns Dialog state, field setters, and async goal operations.
 */
function useCodexGoalDialogState({
  open,
  conversationId,
  goal,
  onGoalChange,
}: Pick<
  CodexGoalDialogProps,
  "open" | "conversationId" | "goal" | "onGoalChange"
>): CodexGoalDialogState {
  const [objective, setObjective] = useState(goal?.objective ?? "");
  const [tokenBudget, setTokenBudget] = useState(goal?.tokenBudget?.toString() ?? "");
  const [modeDraft, setModeDraftState] = useState<CodexGoalModeDraft>(
    codexGoalModeDraftForGoal(goal),
  );
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [statusUpdating, setStatusUpdating] = useState<CodexGoalStatusUpdate | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refreshGoal = useCallback(async () => {
    if (!conversationId) return;
    setLoading(true);
    setError(null);
    try {
      const response = await getCodexGoal(conversationId);
      onGoalChange(response.goal);
      setObjective(response.goal?.objective ?? "");
      setTokenBudget(response.goal?.tokenBudget?.toString() ?? "");
      setModeDraftState(codexGoalModeDraftForGoal(response.goal));
    } catch (err) {
      setError(codexGoalError("Could not read goal", err));
    } finally {
      setLoading(false);
    }
  }, [conversationId, onGoalChange]);

  useEffect(() => {
    if (!open) return;
    void refreshGoal();
  }, [open, refreshGoal]);

  useEffect(() => {
    if (!open) return;
    setObjective(goal?.objective ?? "");
    setTokenBudget(goal?.tokenBudget?.toString() ?? "");
    setModeDraftState(codexGoalModeDraftForGoal(goal));
  }, [goal, open]);

  const setObjectiveDraft = (value: string) => {
    setObjective(value);
    if (error !== null) setError(null);
  };
  const setTokenBudgetDraft = (value: string) => {
    setTokenBudget(value);
    if (error !== null) setError(null);
  };
  const setModeDraft = (value: CodexGoalModeDraft) => {
    setModeDraftState(value);
    if (error !== null) setError(null);
  };

  const saveGoal = async () => {
    if (!conversationId) return;
    const trimmedObjective = objective.trim();
    if (!trimmedObjective) {
      setError("Goal objective cannot be empty.");
      return;
    }
    let parsedBudget: number | null;
    try {
      parsedBudget = parseCodexGoalBudget(tokenBudget);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const response = await setCodexGoal(conversationId, {
        objective: trimmedObjective,
        tokenBudget: parsedBudget,
        status: modeDraft === "keep" ? undefined : modeDraft,
      });
      onGoalChange(response.goal);
      setObjective(response.goal?.objective ?? trimmedObjective);
      setTokenBudget(response.goal?.tokenBudget?.toString() ?? tokenBudget.trim());
      setModeDraftState(codexGoalModeDraftForGoal(response.goal));
    } catch (err) {
      setError(codexGoalError("Could not set goal", err));
    } finally {
      setSaving(false);
    }
  };

  const clearGoal = async () => {
    if (!conversationId) return;
    setClearing(true);
    setError(null);
    try {
      await clearCodexGoal(conversationId);
      onGoalChange(null);
      setObjective("");
      setTokenBudget("");
      setModeDraftState("active");
    } catch (err) {
      setError(codexGoalError("Could not clear goal", err));
    } finally {
      setClearing(false);
    }
  };

  const updateGoalStatus = async (status: CodexGoalStatusUpdate) => {
    if (!conversationId) return;
    setStatusUpdating(status);
    setError(null);
    try {
      const response = await updateCodexGoalStatus(conversationId, status);
      onGoalChange(response.goal);
      setObjective(response.goal?.objective ?? "");
      setTokenBudget(response.goal?.tokenBudget?.toString() ?? "");
      setModeDraftState(codexGoalModeDraftForGoal(response.goal));
    } catch (err) {
      const action = status === "paused" ? "pause" : "resume";
      setError(codexGoalError(`Could not ${action} goal`, err));
    } finally {
      setStatusUpdating(null);
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
    error,
    setObjectiveDraft,
    setTokenBudgetDraft,
    setModeDraft,
    saveGoal,
    clearGoal,
    pauseGoal: () => updateGoalStatus("paused"),
    resumeGoal: () => updateGoalStatus("active"),
  };
}

/**
 * Dialog for viewing, setting, and clearing a Codex-native thread goal.
 *
 * @param props - Dialog state and session goal bindings.
 * @param props.open - Whether the dialog is open.
 * @param props.onOpenChange - Called when the dialog open state changes.
 * @param props.conversationId - Active session id, e.g. ``"conv_abc123"``.
 * @param props.readOnly - ``true`` when write actions must be disabled.
 * @param props.goal - Current goal state, or ``null`` when none is set.
 * @param props.onGoalChange - Called after successful read, set, or clear.
 * @returns Codex goal dialog.
 */
export function CodexGoalDialog({
  open,
  onOpenChange,
  conversationId,
  readOnly,
  goal,
  onGoalChange,
}: CodexGoalDialogProps) {
  const state = useCodexGoalDialogState({
    open,
    conversationId,
    goal,
    onGoalChange,
  });
  const { loading, saving, clearing, statusUpdating } = state;
  const busy = loading || saving || clearing || statusUpdating !== null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg" aria-describedby={undefined}>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <TargetIcon className="size-4" />
            <span>Goal</span>
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-3">
          <CodexGoalSummary loading={loading} goal={goal} />
          <CodexGoalEditor
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

        <CodexGoalActions
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
