import { useCallback, useEffect, useRef, useState } from "react";
import {
  applyCorrectionProposal,
  answerStakeholderQuestion,
  bindDpiaCaseSession,
  DpiaCaseConflictError,
  editCorrectionProposal,
  loadDurableDpiaCase,
  loadDpiaCase,
  recordOfficerDecision,
  replayValidatedInvestigation,
  recordDpiaLiveRun,
  rejectCorrectionProposal,
  resetDpiaCase,
  saveDurableDpiaCase,
  stageCorrectionProposal,
  updateDpiaIntake,
  type DpiaLoadResult,
} from "@/lib/dpia/dpiaApi";
import type {
  CorrectionProposal,
  DpiaCaseSnapshot,
  DpiaLiveRunState,
  OfficerDecision,
} from "@/lib/dpia/types";

export interface DpiaCaseController {
  caseData: DpiaCaseSnapshot;
  source: DpiaLoadResult["source"];
  recoveredInvalidState: boolean;
  isLoading: boolean;
  isSaving: boolean;
  persistenceError: string | null;
  bindSession: (sessionId: string, actor?: string) => Promise<DpiaCaseSnapshot>;
  stageCorrection: (
    proposal: CorrectionProposal,
    source: "agent" | "manual",
  ) => Promise<DpiaCaseSnapshot>;
  editCorrection: (proposalId: string, proposal: CorrectionProposal) => Promise<DpiaCaseSnapshot>;
  applyCorrection: (proposal: CorrectionProposal, actor?: string) => Promise<DpiaCaseSnapshot>;
  rejectCorrection: (proposalId: string, actor?: string) => Promise<DpiaCaseSnapshot>;
  recordLiveRun: (liveRun: DpiaLiveRunState | undefined) => Promise<DpiaCaseSnapshot>;
  updateIntake: (values: Record<string, string>, actor?: string) => Promise<DpiaCaseSnapshot>;
  answerQuestion: (input: {
    questionId: string;
    response: string;
    answeredBy: string;
  }) => Promise<DpiaCaseSnapshot>;
  replaySnapshot: () => Promise<DpiaCaseSnapshot>;
  decide: (
    decision: Omit<OfficerDecision, "processingModelVersion" | "policyPackVersion">,
  ) => Promise<DpiaCaseSnapshot>;
  reset: () => Promise<DpiaCaseSnapshot>;
}

export function useDpiaCase(caseId: string): DpiaCaseController {
  const [loadResult, setLoadResult] = useState(() => loadDpiaCase(caseId));
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [persistenceError, setPersistenceError] = useState<string | null>(null);
  const caseDataRef = useRef(loadResult.caseData);
  const revisionRef = useRef(0);
  const queueRef = useRef<Promise<void>>(Promise.resolve());
  const readyRef = useRef<Promise<void>>(Promise.resolve());
  const queueGenerationRef = useRef(0);
  const pendingWritesRef = useRef(0);
  const activeCaseIdRef = useRef(caseId);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      queueGenerationRef.current += 1;
    };
  }, []);

  useEffect(() => {
    let active = true;
    const generation = queueGenerationRef.current + 1;
    queueGenerationRef.current = generation;
    activeCaseIdRef.current = caseId;
    pendingWritesRef.current = 0;
    setIsLoading(true);
    setIsSaving(false);
    setPersistenceError(null);
    const ready = loadDurableDpiaCase(caseId)
      .then((loaded) => {
        if (!active || generation !== queueGenerationRef.current) return;
        caseDataRef.current = loaded.caseData;
        revisionRef.current = loaded.revision;
        setLoadResult(loaded);
      })
      .catch((error: unknown) => {
        if (!active || generation !== queueGenerationRef.current) return;
        setPersistenceError(error instanceof Error ? error.message : "DPIA case loading failed.");
        throw error;
      })
      .finally(() => {
        if (active && generation === queueGenerationRef.current) setIsLoading(false);
      });
    readyRef.current = ready;
    void ready.catch(() => undefined);
    return () => {
      active = false;
    };
  }, [caseId]);

  const commit = useCallback(
    (mutate: (current: DpiaCaseSnapshot) => DpiaCaseSnapshot) => {
      const generation = queueGenerationRef.current;
      pendingWritesRef.current += 1;
      setIsSaving(true);
      const action = queueRef.current.then(async () => {
        await readyRef.current;
        if (generation !== queueGenerationRef.current) {
          throw new Error("The DPIA case changed before this update could be saved.");
        }
        const current = caseDataRef.current;
        const next = mutate(current);
        if (next === current) return current;
        try {
          const saved = await saveDurableDpiaCase(next, revisionRef.current);
          if (
            generation !== queueGenerationRef.current ||
            activeCaseIdRef.current !== caseId ||
            !mountedRef.current
          ) {
            return saved.caseData;
          }
          caseDataRef.current = saved.caseData;
          revisionRef.current = saved.revision;
          setLoadResult(saved);
          setPersistenceError(null);
          return saved.caseData;
        } catch (error) {
          if (error instanceof DpiaCaseConflictError) {
            if (
              generation !== queueGenerationRef.current ||
              activeCaseIdRef.current !== caseId ||
              !mountedRef.current
            ) {
              throw error;
            }
            const recoveryGeneration = queueGenerationRef.current + 1;
            queueGenerationRef.current = recoveryGeneration;
            pendingWritesRef.current = 0;
            setIsSaving(false);
            setPersistenceError(error.message);
            const reloaded = await loadDurableDpiaCase(caseId);
            if (
              recoveryGeneration !== queueGenerationRef.current ||
              activeCaseIdRef.current !== caseId ||
              !mountedRef.current
            ) {
              throw error;
            }
            caseDataRef.current = reloaded.caseData;
            revisionRef.current = reloaded.revision;
            setLoadResult(reloaded);
          } else {
            setPersistenceError(
              error instanceof Error ? error.message : "The DPIA case update could not be saved.",
            );
          }
          throw error;
        }
      });
      queueRef.current = action.then(
        () => undefined,
        () => undefined,
      );
      return action.finally(() => {
        if (
          generation !== queueGenerationRef.current ||
          activeCaseIdRef.current !== caseId ||
          !mountedRef.current
        ) {
          return;
        }
        pendingWritesRef.current -= 1;
        if (pendingWritesRef.current === 0) setIsSaving(false);
      });
    },
    [caseId],
  );

  const updateIntake = useCallback(
    (values: Record<string, string>, actor = "Alex Morgan") =>
      commit((current) => updateDpiaIntake(current, values, new Date().toISOString(), actor)),
    [commit],
  );

  const bindSession = useCallback(
    (sessionId: string, actor = "Alex Morgan") =>
      commit((current) => bindDpiaCaseSession(current, sessionId, new Date().toISOString(), actor)),
    [commit],
  );

  const stageCorrection = useCallback(
    (proposal: CorrectionProposal, source: "agent" | "manual") =>
      commit((current) =>
        stageCorrectionProposal(current, proposal, source, new Date().toISOString()),
      ),
    [commit],
  );

  const editCorrection = useCallback(
    (proposalId: string, proposal: CorrectionProposal) =>
      commit((current) => editCorrectionProposal(current, proposalId, proposal)),
    [commit],
  );

  const applyCorrection = useCallback(
    (proposal: CorrectionProposal, actor = "Alex Morgan") =>
      commit((current) =>
        applyCorrectionProposal(current, proposal, actor, new Date().toISOString()),
      ),
    [commit],
  );

  const rejectCorrection = useCallback(
    (proposalId: string, actor = "Alex Morgan") =>
      commit((current) =>
        rejectCorrectionProposal(current, proposalId, actor, new Date().toISOString()),
      ),
    [commit],
  );

  const recordLiveRun = useCallback(
    (liveRun: DpiaLiveRunState | undefined) =>
      commit((current) => recordDpiaLiveRun(current, liveRun)),
    [commit],
  );

  const answerQuestion = useCallback(
    (input: { questionId: string; response: string; answeredBy: string }) =>
      commit((current) =>
        answerStakeholderQuestion(current, {
          ...input,
          answeredAt: new Date().toISOString(),
        }),
      ),
    [commit],
  );

  const decide = useCallback(
    (decision: Omit<OfficerDecision, "processingModelVersion" | "policyPackVersion">) =>
      commit((current) =>
        recordOfficerDecision(current, {
          ...decision,
          processingModelVersion: current.processingModel.version,
          policyPackVersion: current.policyPack.version,
        }),
      ),
    [commit],
  );

  const replaySnapshot = useCallback(
    () => commit((current) => replayValidatedInvestigation(current, new Date().toISOString())),
    [commit],
  );

  const reset = useCallback(() => commit(() => resetDpiaCase(caseId)), [caseId, commit]);

  return {
    caseData: loadResult.caseData,
    source: loadResult.source,
    recoveredInvalidState: loadResult.recoveredInvalidState,
    isLoading,
    isSaving,
    persistenceError,
    bindSession,
    stageCorrection,
    editCorrection,
    applyCorrection,
    rejectCorrection,
    recordLiveRun,
    updateIntake,
    answerQuestion,
    replaySnapshot,
    decide,
    reset,
  };
}
