import { type RefObject, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { AlertTriangleIcon, Loader2Icon } from "lucide-react";
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
import { EFFORT_SELECT_NONE } from "@/components/HarnessConfigControls";
import { showToast } from "@/components/ui/toast";
import { WorkspacePickerDialog } from "./WorkspacePickerDialog";
import { WorkspacePathField } from "./WorkspacePathField";
import {
  chooseBinding,
  type ExecutionAccount,
  type ExecutionBinding,
  type ExecutionMachine,
  type ExecutionTopology,
} from "@/hooks/useExecutionTopology";
import { checkHostDirectory } from "@/hooks/useHostFilesystem";
import { fetchHostModelOptions, fetchHosts, useHostModelOptions, useHosts } from "@/hooks/useHosts";
import { findNativeModelOption } from "@/lib/codexNativeModels";
import {
  effortChoicesForModel,
  effortLabel,
  harnessLabel,
  modelApplyState,
  modelOptionLabel,
  planSessionSwitch,
  routingOn,
  sessionIsBusy,
  sessionModelId,
  switchBaselineChanged,
  switchNeedsLaunch,
  type SwitchDraft,
  type SwitchHarness,
  type SwitchReassert,
  type SwitchStep,
} from "@/lib/sessionSwitch";
import { getSessionSlim, launchRunner, updateSession } from "@/lib/sessionsApi";
import { terminalsQueryKey, type TerminalInfo } from "@/lib/terminals";
import type { NativeModelOption, Session } from "@/lib/types";
import { useChatStore } from "@/store/chatStore";
import { useSwitchWorkspace } from "./useSwitchWorkspace";

export interface SwitchDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  sessionId: string;
  currentHostId: string | null;
  currentWorkspace?: string | null;
  /** Element to focus on close, for openers that aren't a Radix trigger. */
  returnFocusRef?: RefObject<HTMLElement | null>;
}

/**
 * Close handler for a dialog opened from outside a Radix trigger, which would
 * otherwise drop focus on the body.
 */
export function returnFocusOnClose(ref: RefObject<HTMLElement | null> | undefined) {
  return (event: Event) => {
    const target = ref?.current;
    if (!target?.isConnected) return;
    event.preventDefault();
    target.focus();
  };
}

/** Cache key of the always-fresh session read this dialog plans against. */
export function switchSnapshotKey(sessionId: string): readonly string[] {
  return ["session-switch-snapshot", sessionId];
}

const NO_ACCOUNTS: ExecutionAccount[] = [];
const NO_BINDINGS: ExecutionBinding[] = [];
const NO_MACHINES: ExecutionMachine[] = [];

/** Thrown for a failed check that ran before any mutation. */
class PreflightError extends Error {}

interface SwitchFailure {
  message: string;
  /** Whether a mutating call was attempted before the failure. */
  mutated: boolean;
  /** The session as re-read after the failure; null when that read failed. */
  after: Session | null;
}

/**
 * Switch a session's machine, account, model and effort within its own
 * harness (moving between providers is a fork). Apply checks everything first,
 * then moves in order or updates in place; a retry resumes a recorded move.
 */
export function SwitchAccountDialog({
  open,
  onOpenChange,
  sessionId,
  currentHostId,
  currentWorkspace: workspaceProp = null,
  returnFocusRef,
  snapshot,
  harness,
  topology,
  pausePollingRef,
  recheckSnapshot,
}: SwitchDialogProps & {
  snapshot: Session;
  harness: SwitchHarness;
  topology: ExecutionTopology;
  /** Set while Apply runs, so the caller's polling doesn't race its reads. */
  pausePollingRef: { current: boolean };
  recheckSnapshot: () => Promise<Session | undefined>;
}) {
  const queryClient = useQueryClient();
  const snapshotKey = useMemo(() => switchSnapshotKey(sessionId), [sessionId]);
  const { data: hosts } = useHosts({ enabled: open });
  const markRunnerLaunched = useChatStore((s) => s.markRunnerLaunched);

  // Routes for this session's harness only; a session keeps its harness.
  const bindings = useMemo(
    () => (topology.bindings ?? NO_BINDINGS).filter((b) => b.harness === harness),
    [topology.bindings, harness],
  );
  const accounts = useMemo(
    () => (topology.accounts ?? NO_ACCOUNTS).filter((a) => a.harness === harness),
    [topology.accounts, harness],
  );
  const machines = useMemo(
    () =>
      (topology.machines ?? NO_MACHINES).filter((m) => bindings.some((b) => b.machine_id === m.id)),
    [topology.machines, bindings],
  );
  const showMachine = machines.length > 1;

  const [selectedMachineId, setSelectedMachineId] = useState<string | null>(null);
  const [selectedAccountId, setSelectedAccountId] = useState<string | null>(null);
  const [selectedHostId, setSelectedHostId] = useState<string | null>(null);
  const [selectionNotice, setSelectionNotice] = useState<string | null>(null);
  const [modelChoice, setModelChoice] = useState<string | null>(null);
  const [effortChoice, setEffortChoice] = useState<string | null>(null);
  const [initialized, setInitialized] = useState(false);
  const [browsing, setBrowsing] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [phase, setPhase] = useState<string | null>(null);
  const [failure, setFailure] = useState<SwitchFailure | null>(null);
  // Fields of a same-host update that failed; the retry re-sends them even
  // when the stored values already match.
  const [reassert, setReassert] = useState<SwitchReassert>({});
  const submittingRef = useRef(false);
  const transitionRef = useRef<string | undefined>(undefined);
  const draftTransitionRef = useRef<string | undefined>(undefined);

  const currentWorkspace = workspaceProp ?? snapshot.accountTransition?.workspace ?? null;
  const sessionHostId = snapshot.hostId ?? currentHostId;
  const pending = snapshot.accountTransition ?? null;
  const onlineHosts = useMemo(() => (hosts ?? []).filter((h) => h.status === "online"), [hosts]);

  const currentBinding = bindings.find((b) => b.host_id === sessionHostId);
  const currentAccount = accounts.find((a) => a.id === currentBinding?.account_id);
  const selectedAccount = accounts.find((a) => a.id === selectedAccountId);
  const selectedBinding = bindings.find(
    (b) =>
      b.host_id === selectedHostId &&
      b.account_id === selectedAccountId &&
      b.machine_id === selectedMachineId,
  );
  // A recorded destination is never swapped for a duplicate route.
  const availableConnection =
    !pending && selectedBinding?.available === false
      ? chooseBinding(
          bindings,
          selectedBinding.account_id,
          selectedHostId,
          harness,
          selectedBinding.machine_id,
          selectedBinding.machine_id,
        )
      : null;
  const machineAccounts = accounts.filter((a) =>
    bindings.some((b) => b.machine_id === selectedMachineId && b.account_id === a.id),
  );

  function selectMachine(machineId: string): void {
    const binding = selectedAccount
      ? chooseBinding(
          bindings,
          selectedAccount.id,
          sessionHostId,
          harness,
          currentBinding?.machine_id,
          machineId,
        )
      : null;
    setSelectedMachineId(machineId);
    setSelectedHostId(binding?.host_id ?? null);
    if (binding) {
      setSelectionNotice(null);
    } else {
      setSelectedAccountId(null);
      setSelectionNotice(
        "The account is unavailable on this machine. Choose an account. Your model and effort are kept for validation.",
      );
    }
  }

  function selectAccount(accountId: string): void {
    if (!selectedMachineId) return;
    const binding = chooseBinding(
      bindings,
      accountId,
      sessionHostId,
      harness,
      currentBinding?.machine_id,
      selectedMachineId,
    );
    setSelectedAccountId(accountId);
    setSelectedHostId(binding?.host_id ?? null);
    setSelectionNotice(null);
  }

  // Seed the draft from the session (or its recorded destination) once, and
  // again when a transition recorded elsewhere appears. Exact bindings are
  // kept, including offline or pending routes.
  useEffect(() => {
    if (
      !open ||
      (initialized && (!pending || pending.id === draftTransitionRef.current)) ||
      submitting ||
      hosts === undefined
    ) {
      return;
    }
    const binding = pending
      ? bindings.find(
          (b) =>
            b.host_id === pending.host_id &&
            b.harness === pending.harness &&
            b.account_id === pending.account_id,
        )
      : currentBinding;
    setSelectedAccountId(pending?.account_id ?? binding?.account_id ?? null);
    setSelectedMachineId(binding?.machine_id ?? (machines.length === 1 ? machines[0].id : null));
    setSelectedHostId(pending?.host_id ?? binding?.host_id ?? null);
    setModelChoice(pending?.model ?? sessionModelId(snapshot));
    setEffortChoice(pending ? pending.effort : (snapshot.reasoningEffort ?? null));
    transitionRef.current = pending?.id;
    draftTransitionRef.current = pending?.id;
    setSelectionNotice(null);
    setInitialized(true);
  }, [open, initialized, submitting, hosts, pending, bindings, currentBinding, machines, snapshot]);

  const selectedHost = onlineHosts.find((h) => h.host_id === selectedHostId);
  const hostName = selectedHost?.name ?? "the new machine";
  const movesHost = switchNeedsLaunch(snapshot, selectedHostId) || pending !== null;

  const catalogQuery = useHostModelOptions(
    selectedHost ? selectedHost.host_id : null,
    harness,
    open && selectedHost !== undefined && selectedAccount !== undefined,
    { accountId: selectedAccount?.id },
  );
  // The origin's catalog labels the "Now" line.
  const { data: currentCatalog } = useHostModelOptions(sessionHostId ?? null, harness, open, {
    poll: false,
    accountId: currentAccount?.id,
  });
  const catalog: readonly NativeModelOption[] | undefined = catalogQuery.data;
  const catalogReady = catalog !== undefined && catalog.length > 0;
  const selectedOption = catalog ? findNativeModelOption(catalog, modelChoice) : null;
  const effortChoices = effortChoicesForModel(harness, selectedOption);
  const effortValid = effortChoice === null || effortChoices.includes(effortChoice);

  // Only a move needs a directory on the destination.
  const directory = useSwitchWorkspace({
    open,
    hostId: movesHost ? selectedHostId : null,
    currentWorkspace,
  });
  const { path: workspaceTrimmed, valid: workspaceValid, addRecent } = directory;

  function commitWorkspacePath(path: string): void {
    directory.onChange(path);
    setBrowsing(true);
  }

  function handleOpenChange(next: boolean): void {
    if (!next) {
      // Mid-apply the outcome is still unknown; keep the dialog up so the
      // result (or the recovery copy) is seen.
      if (submittingRef.current) return;
      setSelectedHostId(null);
      setSelectedAccountId(null);
      setSelectedMachineId(null);
      setSelectionNotice(null);
      transitionRef.current = undefined;
      draftTransitionRef.current = undefined;
      setModelChoice(null);
      setEffortChoice(null);
      setInitialized(false);
      directory.reset();
      setBrowsing(false);
      setFailure(null);
      setReassert({});
      setPhase(null);
      setSubmitting(false);
    }
    onOpenChange(next);
  }

  const draft: SwitchDraft | null =
    selectedHostId === null || selectedAccount === undefined
      ? null
      : {
          accountId: selectedAccount.id,
          hostId: selectedHostId,
          harness,
          model: modelChoice,
          effort: effortChoice,
          workspace: movesHost ? workspaceTrimmed : null,
        };

  // Plan against the live snapshot so a no-op shows before Apply.
  let plannedSteps: SwitchStep[] | null = null;
  if (
    draft !== null &&
    catalogReady &&
    selectedOption !== null &&
    effortValid &&
    (!movesHost || workspaceValid)
  ) {
    try {
      plannedSteps = planSessionSwitch(snapshot, draft, catalog ?? [], reassert);
    } catch {
      plannedSteps = null;
    }
  }

  // Same host, draft equals the stored request: is it active?
  const requestState =
    !movesHost &&
    catalog !== undefined &&
    selectedOption !== null &&
    findNativeModelOption(catalog, snapshot.modelOverride)?.id === selectedOption.id
      ? modelApplyState(snapshot, catalog, selectedOption.id)
      : null;

  const stateUnknown = failure?.mutated === true && failure.after === null;

  let blocker: string | null = null;
  if (!initialized) blocker = "Loading the session…";
  else if (showMachine && !selectedMachineId) blocker = "Choose a machine.";
  else if (!selectedAccount) blocker = "Choose an account.";
  else if (stateUnknown) blocker = "Check the session's state before trying again.";
  else if (sessionIsBusy(snapshot))
    blocker = "The session is busy. Apply unlocks when the current turn finishes.";
  else if (!selectedBinding?.available)
    blocker =
      selectedBinding?.reason ??
      "This account route is unavailable. Choose an available machine or account.";
  else if (selectedHost === undefined) blocker = "Choose an online host.";
  else if (catalogQuery.isPending) blocker = "Loading models…";
  else if (catalogQuery.isError) blocker = "The model list didn't load.";
  else if (!catalogReady) blocker = "This account reports no models.";
  else if (selectedOption === null) blocker = "Choose a model.";
  else if (!effortValid) blocker = "Choose an effort for this model.";
  else if (movesHost && !workspaceValid) blocker = "Choose a working directory.";
  else if (plannedSteps === null) blocker = "This combination can't be applied.";
  else if (plannedSteps.length === 0)
    blocker =
      requestState === "pending"
        ? "The requested model is set. It becomes active when the session reports it."
        : "Already using this account, machine, model and effort.";
  const canApply = blocker === null && !submitting;

  async function runStep(step: SwitchStep): Promise<void> {
    switch (step.kind) {
      case "begin-transition": {
        const recorded = await updateSession(sessionId, {
          runnerId: "",
          accountId: step.draft.accountId,
          targetHostId: step.draft.hostId,
          targetHarness: step.draft.harness,
          targetModel: step.draft.model ?? undefined,
          targetEffort: step.draft.effort,
          targetWorkspace: step.draft.workspace ?? undefined,
          silent: true,
        });
        transitionRef.current = recorded.accountTransition?.id;
        if (!transitionRef.current) {
          throw new Error("The server did not record the account change.");
        }
        return;
      }
      case "update":
        await updateSession(sessionId, {
          ...step.updates,
          ...(transitionRef.current ? { transitionId: transitionRef.current } : {}),
        });
        return;
      case "launch":
        await launchRunner(step.hostId, sessionId, step.workspace, undefined, {
          accountId: draft!.accountId,
          transitionId: transitionRef.current,
        });
        transitionRef.current = undefined;
        return;
    }
  }

  function phaseFor(step: SwitchStep): string {
    switch (step.kind) {
      case "begin-transition":
        return "Releasing the current runner…";
      case "update":
        return "Applying model settings…";
      case "launch":
        return `Starting on ${hostName}…`;
    }
  }

  async function handleCancelMove(): Promise<void> {
    if (submittingRef.current || pending === null) return;
    submittingRef.current = true;
    pausePollingRef.current = true;
    setSubmitting(true);
    setFailure(null);
    try {
      await updateSession(sessionId, { transitionId: pending.id, abandonTransition: true });
      queryClient.setQueryData(snapshotKey, await getSessionSlim(sessionId));
      // Seed the pickers again from the now-detached session.
      transitionRef.current = undefined;
      draftTransitionRef.current = undefined;
      setReassert({});
      setInitialized(false);
    } catch (e) {
      const message = e instanceof Error && e.message ? e.message : "Couldn't cancel the move.";
      setFailure({ message, mutated: false, after: null });
    } finally {
      submittingRef.current = false;
      pausePollingRef.current = false;
      setSubmitting(false);
    }
  }

  async function handleApply(): Promise<void> {
    if (submittingRef.current || !canApply || draft === null) return;
    submittingRef.current = true;
    pausePollingRef.current = true;
    setSubmitting(true);
    setFailure(null);
    let mutated = false;
    let launched = false;
    let failedUpdate: SwitchStep | null = null;
    try {
      setPhase(`Checking ${hostName}…`);
      // Every check runs before the first mutation; any failure here leaves
      // the session exactly as it was.
      const first = await getSessionSlim(sessionId);
      transitionRef.current = first.accountTransition?.id;
      queryClient.setQueryData(snapshotKey, first);
      if (sessionIsBusy(first)) {
        throw new PreflightError("The session started a turn. Try again when it finishes.");
      }
      const liveHosts = await fetchHosts(false);
      if (liveHosts.find((h) => h.host_id === draft.hostId)?.status !== "online") {
        throw new PreflightError(`${hostName} is offline. Choose another machine.`);
      }
      const rows = await fetchHostModelOptions(draft.hostId, harness, draft.accountId);
      queryClient.setQueryData(
        ["host-model-options", draft.hostId, harness, draft.accountId],
        rows,
      );
      const option = findNativeModelOption(rows, draft.model);
      if (option === null) {
        throw new PreflightError("This account no longer offers the model. Choose another.");
      }
      if (draft.effort !== null && !effortChoicesForModel(harness, option).includes(draft.effort)) {
        throw new PreflightError("This effort isn't available for the model. Choose another.");
      }
      if (switchNeedsLaunch(first, draft.hostId) || first.accountTransition) {
        const problem = await checkHostDirectory(draft.hostId, draft.workspace ?? "");
        if (problem !== null) throw new PreflightError(problem);
      }
      // The probes above can take seconds. Re-read right before planning so
      // a turn that started, or a change made elsewhere, stops the switch.
      const fresh = await getSessionSlim(sessionId);
      queryClient.setQueryData(snapshotKey, fresh);
      if (sessionIsBusy(fresh)) {
        throw new PreflightError("The session started a turn. Try again when it finishes.");
      }
      if (switchBaselineChanged(first, fresh)) {
        throw new PreflightError(
          "The session changed while it was being checked. Review its current state and try again.",
        );
      }
      let steps: SwitchStep[];
      try {
        steps = planSessionSwitch(fresh, draft, rows, reassert);
      } catch (e) {
        throw new PreflightError(e instanceof Error ? e.message : String(e));
      }

      // The calls depend on each other's effects, so they run in order.
      /* oxlint-disable no-await-in-loop */
      for (const step of steps) {
        setPhase(phaseFor(step));
        mutated = true;
        if (step.kind === "update") failedUpdate = step;
        await runStep(step);
        failedUpdate = null;
        if (step.kind === "launch") launched = true;
      }
      /* oxlint-enable no-await-in-loop */
      setReassert({});

      if (launched) {
        // Nothing on the wire says the new runner is booting yet; without
        // this the session reads as idle on a silent, empty chat.
        markRunnerLaunched();
        if (draft.workspace) addRecent(draft.workspace);
      }
      const summary = `${modelOptionLabel(option)} · ${effortLabel(draft.effort)}`;
      submittingRef.current = false;
      handleOpenChange(false);
      void queryClient.invalidateQueries({ queryKey: ["session", sessionId] });
      void queryClient.invalidateQueries({ queryKey: ["session-agent", sessionId] });
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      if (launched) {
        // The terminals cache never goes stale and unions on fetch, so the
        // old host's shells would linger without an explicit clear.
        queryClient.setQueryData<TerminalInfo[]>(terminalsQueryKey(sessionId), []);
        void queryClient.invalidateQueries({ queryKey: terminalsQueryKey(sessionId) });
      }
      if (steps.length > 0) {
        showToast(
          launched ? `Moving to ${hostName}. Requested ${summary}.` : `Requested ${summary}.`,
        );
      }
      // The composer reads model and routing from the chat store; refresh it
      // so it shows the request instead of the old model or Smart Routing.
      void Promise.resolve()
        .then(() => useChatStore.getState().refreshSessionState(sessionId))
        .catch(() => undefined);
    } catch (e) {
      const message =
        e instanceof Error && e.message ? e.message : "Couldn't apply the change. Try again.";
      let after: Session | null = null;
      if (mutated) {
        // No rollback exists: show the state the server actually holds and
        // plan any retry from it.
        try {
          after = await getSessionSlim(sessionId);
          queryClient.setQueryData(snapshotKey, after);
        } catch {
          after = null;
        }
      }
      // A failed same-host update may have been stored without reaching the
      // harness; re-send its fields next time.
      if (failedUpdate?.kind === "update" && !("runnerId" in failedUpdate.updates)) {
        const { updates } = failedUpdate;
        setReassert((prev) => ({
          model: prev.model || "modelOverride" in updates,
          effort: prev.effort || "reasoningEffort" in updates,
        }));
      }
      setFailure({ message, mutated, after });
    } finally {
      submittingRef.current = false;
      pausePollingRef.current = false;
      setSubmitting(false);
      setPhase(null);
    }
  }

  // "Now" shows the model the session REPORTS; a stored request that
  // differs is named separately rather than presented as active.
  const labelFor = (id: string | null | undefined): string | null => {
    if (!id) return null;
    const option = currentCatalog ? findNativeModelOption(currentCatalog, id) : null;
    return option ? modelOptionLabel(option) : id;
  };
  const reportedText = labelFor(snapshot.llmModel);
  const requestedText =
    snapshot.modelOverride && labelFor(snapshot.modelOverride) !== reportedText
      ? labelFor(snapshot.modelOverride)
      : null;
  const currentModelText = routingOn(snapshot)
    ? "Smart Routing"
    : (reportedText ?? "model not reported yet");

  const catalogSource = selectedOption?.source;
  const workspaceChanges =
    movesHost && workspaceValid && !!currentWorkspace && workspaceTrimmed !== currentWorkspace;
  const locked = submitting || pending !== null;

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        data-testid="switch-host-dialog"
        onCloseAutoFocus={returnFocusOnClose(returnFocusRef)}
        className="flex max-h-[85dvh] flex-col gap-0 overflow-hidden p-0 sm:max-w-lg"
      >
        <DialogHeader className="shrink-0 px-6 pt-6 pb-4">
          <DialogTitle>Switch model or account</DialogTitle>
          <DialogDescription>
            Choose the account and model for this session. The conversation stays here.
          </DialogDescription>
        </DialogHeader>

        <div
          className="flex min-h-0 flex-col gap-4 overflow-y-auto overscroll-contain px-6 pb-6"
          data-testid="switch-scroll-body"
        >
          <p
            className="shrink-0 break-words text-xs text-muted-foreground"
            data-testid="switch-current-summary"
          >
            Now: {pending ? "Move interrupted" : (currentAccount?.label ?? "Unregistered account")}
            {" · "}
            {harnessLabel(harness)} · {currentModelText} ·{" "}
            {effortLabel(snapshot.reasoningEffort ?? null)}
            {requestedText !== null && !routingOn(snapshot) && (
              <span data-testid="switch-current-requested">
                {" "}
                (requested {requestedText}, not active yet)
              </span>
            )}
          </p>

          {!initialized ? (
            <p
              className="flex items-center gap-2 text-xs text-muted-foreground"
              data-testid="switch-loading"
            >
              <Loader2Icon className="size-3.5 animate-spin" aria-hidden />
              Loading the session…
            </p>
          ) : (
            <>
              <div className="flex flex-col gap-3">
                {showMachine && (
                  <div className="flex flex-col gap-2">
                    <label
                      htmlFor="switch-machine-select"
                      className="text-xs font-medium text-muted-foreground"
                    >
                      Machine
                    </label>
                    <Select
                      value={selectedMachineId ?? ""}
                      componentId="switch_host.machine"
                      disabled={locked}
                      onValueChange={selectMachine}
                    >
                      <SelectTrigger
                        id="switch-machine-select"
                        data-testid="switch-machine-select"
                        className="w-full text-xs"
                      >
                        <SelectValue placeholder="Choose a machine" />
                      </SelectTrigger>
                      <SelectContent>
                        {machines.map((machine) => {
                          const available = bindings.some(
                            (b) => b.machine_id === machine.id && b.available,
                          );
                          return (
                            <SelectItem
                              key={machine.id}
                              value={machine.id}
                              data-testid={`switch-machine-option-${machine.id}`}
                              disabled={!available}
                            >
                              {machine.label}
                              {available ? "" : " (unavailable)"}
                            </SelectItem>
                          );
                        })}
                      </SelectContent>
                    </Select>
                  </div>
                )}
                <div className="flex flex-col gap-2">
                  <label
                    htmlFor="switch-account-select"
                    className="text-xs font-medium text-muted-foreground"
                  >
                    Account
                  </label>
                  <Select
                    value={selectedAccountId ?? ""}
                    componentId="switch_host.account"
                    disabled={locked || !selectedMachineId}
                    onValueChange={selectAccount}
                  >
                    <SelectTrigger
                      id="switch-account-select"
                      data-testid="switch-account-select"
                      className="w-full text-xs"
                    >
                      <SelectValue placeholder="Choose an account" />
                    </SelectTrigger>
                    <SelectContent>
                      {machineAccounts.map((account) => {
                        const routes = bindings.filter(
                          (b) => b.machine_id === selectedMachineId && b.account_id === account.id,
                        );
                        const available = routes.some((b) => b.available);
                        return (
                          <SelectItem
                            key={account.id}
                            value={account.id}
                            data-testid={`switch-account-option-${account.id}`}
                            disabled={!available}
                          >
                            {account.label}
                            {available ? "" : ` (${routes[0]?.reason ?? "unavailable"})`}
                          </SelectItem>
                        );
                      })}
                    </SelectContent>
                  </Select>
                </div>
                {selectionNotice && (
                  <p
                    role="status"
                    className="text-xs text-warning"
                    data-testid="switch-selection-notice"
                  >
                    {selectionNotice}
                  </p>
                )}
                {availableConnection && (
                  <div
                    className="flex flex-col items-start gap-2 text-xs"
                    data-testid="switch-connection-recovery"
                  >
                    <p className="text-muted-foreground">
                      Another connection for this account is available on this machine.
                    </p>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={submitting}
                      data-testid="switch-use-available-connection"
                      onClick={() => {
                        setSelectedHostId(availableConnection.host_id);
                        setSelectionNotice(
                          "An available connection is selected on this machine. Your model and effort will be checked against its catalog before Apply.",
                        );
                      }}
                    >
                      Use available connection
                    </Button>
                  </div>
                )}
                {selectedBinding && (
                  <details
                    className="text-xs text-muted-foreground"
                    data-testid="switch-connection-details"
                  >
                    <summary className="cursor-pointer">Connection details</summary>
                    <div
                      className="mt-2 flex flex-col gap-1 break-words"
                      data-testid="switch-route-detail"
                    >
                      <span>
                        Host:{" "}
                        {(hosts ?? []).find((h) => h.host_id === selectedBinding.host_id)?.name ??
                          "Unavailable host"}
                      </span>
                      <span>Agent: {harnessLabel(harness)}</span>
                      <span>
                        Route ID: <code>{selectedBinding.host_id}</code>
                      </span>
                      {!selectedBinding.available && (
                        <span>{selectedBinding.reason ?? "Unavailable"}</span>
                      )}
                    </div>
                  </details>
                )}
                {pending && (
                  <p
                    role="status"
                    className="text-xs text-muted-foreground"
                    data-testid="switch-pending-notice"
                  >
                    This move was interrupted. Apply resumes it on the recorded machine, or cancel
                    the move to choose again.
                  </p>
                )}
              </div>

              {selectedHost !== undefined && selectedAccount !== undefined && (
                <div className="grid gap-3 sm:grid-cols-2" data-testid="switch-model-effort">
                  <div className="flex min-w-0 flex-col gap-2">
                    <label
                      htmlFor="switch-model-select"
                      className="text-xs font-medium text-muted-foreground"
                    >
                      Model
                    </label>
                    {catalogQuery.isPending ? (
                      <p
                        className="flex h-8 items-center gap-2 text-xs text-muted-foreground"
                        data-testid="switch-catalog-loading"
                        aria-live="polite"
                      >
                        <Loader2Icon className="size-3.5 animate-spin" aria-hidden />
                        Loading models from {hostName}…
                      </p>
                    ) : catalogQuery.isError || !catalogReady ? (
                      <div
                        className="flex flex-col items-start gap-1.5 text-xs"
                        data-testid={
                          catalogQuery.isError ? "switch-catalog-error" : "switch-catalog-empty"
                        }
                      >
                        <span
                          className={
                            catalogQuery.isError ? "text-destructive" : "text-muted-foreground"
                          }
                        >
                          {catalogQuery.isError
                            ? `Couldn't load models from ${hostName}: ${catalogQuery.error.message}`
                            : `${hostName} reports no ${harnessLabel(harness)} models.`}
                        </span>
                        <Button
                          variant="outline"
                          size="sm"
                          data-testid="switch-catalog-retry"
                          disabled={catalogQuery.isFetching || submitting}
                          onClick={() => void catalogQuery.refetch()}
                        >
                          {catalogQuery.isFetching ? "Retrying…" : "Retry"}
                        </Button>
                      </div>
                    ) : (
                      <>
                        <Select
                          value={selectedOption?.id ?? ""}
                          onValueChange={(v) => setModelChoice(v)}
                          componentId="switch_host.model"
                          valueHasNoPii
                          disabled={locked}
                        >
                          <SelectTrigger
                            id="switch-model-select"
                            className="w-full text-xs"
                            data-testid="switch-model-select"
                          >
                            <SelectValue placeholder="Choose a model" />
                          </SelectTrigger>
                          <SelectContent position="popper" align="start">
                            {catalog.map((option) => (
                              <SelectItem
                                key={option.id}
                                value={option.id}
                                data-testid={`switch-model-option-${option.id}`}
                                className="text-xs"
                              >
                                {modelOptionLabel(option)}
                                {option.isDefault ? " (default)" : ""}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                        {selectedOption === null && modelChoice !== null && (
                          <p
                            className="text-xs text-warning"
                            data-testid="switch-model-unsupported"
                            role="status"
                          >
                            {modelChoice} isn't offered here. Choose a model.
                          </p>
                        )}
                        {requestState === "unapplied" && snapshot.llmModel && (
                          <p
                            className="text-xs text-warning"
                            data-testid="switch-model-unapplied"
                            role="status"
                          >
                            The session reports {labelFor(snapshot.llmModel)}, not this request.
                            Apply sends it again.
                          </p>
                        )}
                        {requestState === "pending" && (
                          <p
                            className="text-xs text-muted-foreground"
                            data-testid="switch-model-pending"
                            role="status"
                          >
                            Requested, not reported as active yet.
                          </p>
                        )}
                        {catalogSource && (
                          <p className="truncate text-xs text-muted-foreground">
                            Via {catalogSource.label}
                            {catalogSource.name ? ` · ${catalogSource.name}` : ""}
                          </p>
                        )}
                      </>
                    )}
                  </div>

                  {catalogReady && selectedOption !== null && (
                    <div className="flex min-w-0 flex-col gap-2">
                      <label
                        htmlFor="switch-effort-select"
                        className="text-xs font-medium text-muted-foreground"
                      >
                        Effort
                      </label>
                      <Select
                        value={
                          effortChoice === null
                            ? EFFORT_SELECT_NONE
                            : effortValid
                              ? effortChoice
                              : ""
                        }
                        onValueChange={(v) => setEffortChoice(v === EFFORT_SELECT_NONE ? null : v)}
                        componentId="switch_host.effort"
                        valueHasNoPii
                        disabled={locked}
                      >
                        <SelectTrigger
                          id="switch-effort-select"
                          className="w-full text-xs"
                          data-testid="switch-effort-select"
                        >
                          <SelectValue placeholder="Choose an effort" />
                        </SelectTrigger>
                        <SelectContent position="popper" align="start">
                          <SelectItem
                            value={EFFORT_SELECT_NONE}
                            data-testid="switch-effort-option-default"
                            className="text-xs"
                          >
                            Default
                            {selectedOption.defaultReasoningEffort
                              ? ` (${effortLabel(selectedOption.defaultReasoningEffort)})`
                              : ""}
                          </SelectItem>
                          {effortChoices.map((effort) => (
                            <SelectItem
                              key={effort}
                              value={effort}
                              data-testid={`switch-effort-option-${effort}`}
                              className="text-xs"
                            >
                              {effortLabel(effort)}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                      {!effortValid && (
                        <p
                          className="text-xs text-warning"
                          data-testid="switch-effort-unsupported"
                          role="status"
                        >
                          {effortLabel(effortChoice)} isn't available for this model. Choose
                          another.
                        </p>
                      )}
                    </div>
                  )}
                </div>
              )}

              {movesHost && selectedHostId !== null && (
                <div className="flex flex-col gap-2">
                  <span className="text-xs font-medium text-muted-foreground">
                    Working directory
                  </span>
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
                  {workspaceChanges ? (
                    <p
                      className="flex items-start gap-1.5 text-xs text-warning"
                      data-testid="switch-host-warning"
                    >
                      <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                      <span className="min-w-0 break-words">
                        The agent continues in {workspaceTrimmed} instead of {currentWorkspace}.
                        Files don't move between machines.
                      </span>
                    </p>
                  ) : selectedHostId !== sessionHostId ? (
                    <p
                      className="text-xs text-muted-foreground"
                      data-testid="switch-host-files-note"
                    >
                      Files don't move between machines. Uncommitted changes stay on the old
                      machine.
                    </p>
                  ) : null}
                </div>
              )}

              {failure !== null && (
                <FailureNotice
                  failure={failure}
                  onRecheck={() =>
                    void recheckSnapshot().then((fresh) => {
                      if (fresh) setFailure((f) => (f === null ? f : { ...f, after: fresh }));
                    })
                  }
                />
              )}

              {blocker !== null && !submitting && failure === null && (
                <p className="text-xs text-muted-foreground" data-testid="switch-blocker">
                  {blocker}
                </p>
              )}
              {phase !== null && (
                <p
                  className="flex items-center gap-2 text-xs text-muted-foreground"
                  data-testid="switch-progress"
                  aria-live="polite"
                >
                  <Loader2Icon className="size-3.5 animate-spin" aria-hidden />
                  {phase}
                </p>
              )}
            </>
          )}
        </div>
        {initialized && (
          <DialogFooter
            className="mx-0 mb-0 shrink-0 flex-row flex-wrap justify-end rounded-none px-6 py-4"
            data-testid="switch-actions"
          >
            {pending && (
              <Button
                data-testid="switch-cancel-move-button"
                variant="outline"
                onClick={() => void handleCancelMove()}
                disabled={submitting}
              >
                Cancel move
              </Button>
            )}
            <Button
              data-testid="switch-cancel-button"
              variant="ghost"
              onClick={() => handleOpenChange(false)}
              disabled={submitting}
            >
              Cancel
            </Button>
            <Button
              data-testid="switch-host-button"
              disabled={!canApply}
              loading={submitting}
              onClick={() => void handleApply()}
            >
              {movesHost ? "Move session" : "Apply"}
            </Button>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  );
}

function FailureNotice({ failure, onRecheck }: { failure: SwitchFailure; onRecheck: () => void }) {
  const { after, mutated, message } = failure;
  let lead: string | null = null;
  if (mutated && after === null) {
    lead =
      "Part of the change may have landed, and the session's state couldn't be read back. Check it before trying again.";
  } else if (mutated && after !== null && after.accountTransition) {
    lead =
      "The move was interrupted and the session isn't running anywhere. Apply resumes it, or cancel the move to choose again.";
  } else if (mutated && after !== null && after.runnerId == null) {
    lead =
      "The session left its old machine but didn't start on the new one, so it isn't running anywhere. Pick a machine, including the one it came from, and try again.";
  } else if (mutated) {
    lead = "The change didn't finish. The summary above shows the session's current state.";
  }
  return (
    <div className="flex flex-col items-start gap-1 text-xs" data-testid="switch-host-error">
      {lead !== null ? (
        <>
          <span className="text-destructive">{lead}</span>
          <span className="text-muted-foreground">{message}</span>
        </>
      ) : (
        <span className="text-destructive">{message}</span>
      )}
      {mutated && after === null && (
        <Button variant="outline" size="sm" data-testid="switch-recheck" onClick={onRecheck}>
          Check session state
        </Button>
      )}
    </div>
  );
}
