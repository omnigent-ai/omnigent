import { authenticatedFetch } from "@/lib/identity";
import { createStudentSuccessAlertSeed } from "./seed";
import { validateCorrectionProposal } from "./correctionProposal";
import { dpiaCaseSnapshotSchema, officerDecisionSchema } from "./schemas";
import type { AuditEvent, DpiaCaseSnapshot, DpiaLiveRunState, OfficerDecision } from "./types";

const STORAGE_VERSION = 1;
export const DPIA_CASE_CHANGED_EVENT = "omnigent:dpia-case-changed";

export interface DpiaLoadResult {
  caseData: DpiaCaseSnapshot;
  source: "persisted" | "seed";
  recoveredInvalidState: boolean;
}

export interface DurableDpiaLoadResult extends DpiaLoadResult {
  revision: number;
  createdBy: string;
  updatedBy: string;
  createdAt: number;
  updatedAt: number;
}

interface DurableDpiaCaseEnvelope {
  case_id: string;
  revision: number;
  snapshot: unknown;
  created_by: string;
  updated_by: string;
  created_at: number;
  updated_at: number;
}

export class DpiaCaseConflictError extends Error {
  readonly currentRevision: number;

  constructor(currentRevision: number) {
    super(`The DPIA case changed at revision ${currentRevision}.`);
    this.name = "DpiaCaseConflictError";
    this.currentRevision = currentRevision;
  }
}

class DpiaCaseNotFoundError extends Error {}

const durableLoadRequests = new Map<string, Promise<DurableDpiaLoadResult>>();

function parseDurableDpiaCase(value: unknown): DurableDpiaLoadResult {
  if (typeof value !== "object" || value === null) {
    throw new Error("The DPIA case response is invalid.");
  }
  const envelope = value as Partial<DurableDpiaCaseEnvelope>;
  if (
    typeof envelope.case_id !== "string" ||
    !Number.isInteger(envelope.revision) ||
    typeof envelope.created_by !== "string" ||
    typeof envelope.updated_by !== "string" ||
    typeof envelope.created_at !== "number" ||
    typeof envelope.updated_at !== "number"
  ) {
    throw new Error("The DPIA case response is invalid.");
  }
  const caseData = dpiaCaseSnapshotSchema.parse(envelope.snapshot);
  if (caseData.id !== envelope.case_id) {
    throw new Error("The DPIA case response does not match the requested case.");
  }
  return {
    caseData,
    revision: envelope.revision as number,
    createdBy: envelope.created_by,
    updatedBy: envelope.updated_by,
    createdAt: envelope.created_at,
    updatedAt: envelope.updated_at,
    source: "persisted",
    recoveredInvalidState: false,
  };
}

async function durableResponse(response: Response): Promise<DurableDpiaLoadResult> {
  if (response.status === 404) throw new DpiaCaseNotFoundError();
  if (response.status === 409) {
    const body = (await response.json()) as {
      error?: { current_revision?: unknown };
    };
    const currentRevision = body.error?.current_revision;
    throw new DpiaCaseConflictError(typeof currentRevision === "number" ? currentRevision : 0);
  }
  if (!response.ok) {
    throw new Error(`DPIA case persistence failed with status ${response.status}.`);
  }
  return parseDurableDpiaCase(await response.json());
}

export async function fetchDurableDpiaCase(caseId: string): Promise<DurableDpiaLoadResult> {
  const response = await authenticatedFetch(`/v1/dpia/cases/${encodeURIComponent(caseId)}`);
  return durableResponse(response);
}

export async function saveDurableDpiaCase(
  caseData: DpiaCaseSnapshot,
  expectedRevision: number,
): Promise<DurableDpiaLoadResult> {
  const snapshot = dpiaCaseSnapshotSchema.parse(caseData);
  const response = await authenticatedFetch(`/v1/dpia/cases/${encodeURIComponent(snapshot.id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ snapshot, expected_revision: expectedRevision }),
  });
  return durableResponse(response);
}

export function loadDurableDpiaCase(caseId: string): Promise<DurableDpiaLoadResult> {
  const pending = durableLoadRequests.get(caseId);
  if (pending) return pending;

  const request = (async () => {
    try {
      return await fetchDurableDpiaCase(caseId);
    } catch (error) {
      if (!(error instanceof DpiaCaseNotFoundError)) throw error;
      const key = dpiaStorageKey(caseId);
      const hadLegacySnapshot = localStorage.getItem(key) !== null;
      const legacy = loadDpiaCase(caseId);
      const saved = await saveDurableDpiaCase(legacy.caseData, 0);
      if (hadLegacySnapshot) localStorage.removeItem(key);
      return {
        ...saved,
        source: legacy.source,
        recoveredInvalidState: legacy.recoveredInvalidState,
      };
    }
  })();
  durableLoadRequests.set(caseId, request);
  void request.then(
    () => durableLoadRequests.delete(caseId),
    () => durableLoadRequests.delete(caseId),
  );
  return request;
}

const questionFactDependencies: Record<string, string[]> = {
  "q-hosting": ["hosting"],
  "q-subprocessors": ["subprocessors", "international-access"],
  "q-special-category": ["special-category-use"],
  "q-access": ["access-controls"],
  "q-escalation": ["score-influence"],
  "q-bias": ["bias-testing"],
  "q-notice": ["student-notice"],
  "q-retention": ["retention", "deletion-confirmation"],
};

const validatedFindingStatuses: Record<
  string,
  DpiaCaseSnapshot["determinations"][number]["status"]
> = {
  "det-purpose": "confirmed",
  "det-lifecycle": "confirmed",
  "det-legal-basis": "needs-judgement",
  "det-necessity": "needs-judgement",
  "det-harms": "potential-issue",
  "det-vendor": "missing-evidence",
  "det-security": "confirmed",
  "det-transparency-retention": "missing-evidence",
};

export function dpiaStorageKey(caseId: string): string {
  return `omnigent:dpia-case:${caseId}:v${STORAGE_VERSION}`;
}

export function loadDpiaCase(
  caseId: string,
  storage: Pick<Storage, "getItem" | "removeItem"> = localStorage,
): DpiaLoadResult {
  if (caseId !== "student-success-alert") throw new Error(`Unknown DPIA case: ${caseId}`);
  const key = dpiaStorageKey(caseId);
  const persisted = storage.getItem(key);
  if (persisted === null) {
    return {
      caseData: createStudentSuccessAlertSeed(),
      source: "seed",
      recoveredInvalidState: false,
    };
  }

  try {
    const parsed = dpiaCaseSnapshotSchema.parse(JSON.parse(persisted));
    return { caseData: parsed, source: "persisted", recoveredInvalidState: false };
  } catch {
    storage.removeItem(key);
    return {
      caseData: createStudentSuccessAlertSeed(),
      source: "seed",
      recoveredInvalidState: true,
    };
  }
}

export function saveDpiaCase(
  caseData: DpiaCaseSnapshot,
  storage: Pick<Storage, "setItem"> = localStorage,
): DpiaCaseSnapshot {
  const parsed = dpiaCaseSnapshotSchema.parse(caseData);
  storage.setItem(dpiaStorageKey(parsed.id), JSON.stringify(parsed));
  if (typeof window !== "undefined" && storage === window.localStorage) {
    window.dispatchEvent(new CustomEvent(DPIA_CASE_CHANGED_EVENT, { detail: parsed.id }));
  }
  return parsed;
}

export function resetDpiaCase(
  caseId: string,
  storage: Pick<Storage, "removeItem"> = localStorage,
): DpiaCaseSnapshot {
  storage.removeItem(dpiaStorageKey(caseId));
  if (typeof window !== "undefined" && storage === window.localStorage) {
    window.dispatchEvent(new CustomEvent(DPIA_CASE_CHANGED_EVENT, { detail: caseId }));
  }
  return createStudentSuccessAlertSeed();
}

export function recordDpiaCaseEvent(
  caseId: string,
  event: { actor: string; action: string; object: string; timestamp: string; newValue?: string },
): DpiaCaseSnapshot {
  const { caseData } = loadDpiaCase(caseId);
  return saveDpiaCase(
    dpiaCaseSnapshotSchema.parse({
      ...caseData,
      updatedAt: event.timestamp,
      audit: [
        ...caseData.audit,
        {
          id: `audit-request-${event.timestamp}`,
          actor: event.actor,
          role: "Privacy Officer",
          action: event.action,
          object: event.object,
          timestamp: event.timestamp,
          ...(event.newValue === undefined ? {} : { newValue: event.newValue }),
        },
      ],
    }),
  );
}

export function bindDpiaCaseSession(
  caseData: DpiaCaseSnapshot,
  sessionId: string,
  boundAt: string,
  actor: string,
): DpiaCaseSnapshot {
  if (caseData.sessionId === sessionId) return caseData;
  return dpiaCaseSnapshotSchema.parse({
    ...caseData,
    sessionId,
    updatedAt: boundAt,
    audit: [
      ...caseData.audit,
      {
        id: `audit-session-${boundAt}`,
        actor,
        role: "Privacy Officer",
        action: "Connected case agent",
        object: caseData.title,
        timestamp: boundAt,
        priorValue: caseData.sessionId,
        newValue: sessionId,
      },
    ],
  });
}

export function recordDpiaLiveRun(
  caseData: DpiaCaseSnapshot,
  liveRun: DpiaLiveRunState | undefined,
): DpiaCaseSnapshot {
  return dpiaCaseSnapshotSchema.parse({ ...caseData, liveRun });
}

export function stageCorrectionProposal(
  caseData: DpiaCaseSnapshot,
  candidate: unknown,
  source: "agent" | "manual",
  createdAt: string,
): DpiaCaseSnapshot {
  const proposal = validateCorrectionProposal(caseData, candidate);
  const proposalJson = JSON.stringify(proposal);
  const existing = (caseData.correctionProposals ?? []).find(
    (record) => record.status === "pending" && JSON.stringify(record.proposal) === proposalJson,
  );
  if (existing) return caseData;
  return dpiaCaseSnapshotSchema.parse({
    ...caseData,
    correctionProposals: [
      ...(caseData.correctionProposals ?? []),
      {
        id: `correction-${createdAt}`,
        proposal,
        source,
        status: "pending",
        createdAt,
      },
    ],
  });
}

export function editCorrectionProposal(
  caseData: DpiaCaseSnapshot,
  proposalId: string,
  candidate: unknown,
): DpiaCaseSnapshot {
  const proposal = validateCorrectionProposal(caseData, candidate);
  const records = caseData.correctionProposals ?? [];
  if (!records.some(({ id, status }) => id === proposalId && status === "pending")) {
    throw new Error(`Pending correction proposal not found: ${proposalId}`);
  }
  return dpiaCaseSnapshotSchema.parse({
    ...caseData,
    correctionProposals: records.map((record) =>
      record.id === proposalId ? { ...record, proposal } : record,
    ),
  });
}

export function rejectCorrectionProposal(
  caseData: DpiaCaseSnapshot,
  proposalId: string,
  actor: string,
  rejectedAt: string,
): DpiaCaseSnapshot {
  const record = (caseData.correctionProposals ?? []).find(({ id }) => id === proposalId);
  if (!record || record.status !== "pending") {
    throw new Error(`Pending correction proposal not found: ${proposalId}`);
  }
  return dpiaCaseSnapshotSchema.parse({
    ...caseData,
    correctionProposals: (caseData.correctionProposals ?? []).map((candidate) =>
      candidate.id === proposalId
        ? { ...candidate, status: "rejected", resolvedAt: rejectedAt }
        : candidate,
    ),
    audit: [
      ...caseData.audit,
      {
        id: `audit-correction-rejected-${rejectedAt}`,
        actor,
        role: "Privacy Officer",
        action: "Rejected correction proposal",
        object: record.proposal.target_facts.map(({ fact_id }) => fact_id).join(", "),
        timestamp: rejectedAt,
        priorValue: JSON.stringify(record.proposal),
        newValue: "Rejected without changing the case",
      },
    ],
  });
}

export function applyCorrectionProposal(
  caseData: DpiaCaseSnapshot,
  candidate: unknown,
  actor: string,
  appliedAt: string,
): DpiaCaseSnapshot {
  const proposal = validateCorrectionProposal(caseData, candidate);
  const values = Object.fromEntries(
    proposal.target_facts.map(({ fact_id, proposed_value }) => [fact_id, proposed_value]),
  );
  const updated = updateDpiaIntake(caseData, values, appliedAt, actor);
  if (updated.processingModel.version !== proposal.expected_version_bump.to) {
    throw new Error("The correction did not produce the proposal's expected version.");
  }
  const evidenceIds = proposal.new_evidence_refs.map(({ evidence_id }) => evidence_id);
  const targetFactIds = new Set(proposal.target_facts.map(({ fact_id }) => fact_id));
  const records = updated.correctionProposals ?? [];
  return dpiaCaseSnapshotSchema.parse({
    ...updated,
    processingModel: {
      ...updated.processingModel,
      facts: updated.processingModel.facts.map((fact) =>
        targetFactIds.has(fact.id)
          ? { ...fact, evidenceIds: Array.from(new Set([...fact.evidenceIds, ...evidenceIds])) }
          : fact,
      ),
    },
    correctionProposals: records.map((record) =>
      record.status === "pending" && JSON.stringify(record.proposal) === JSON.stringify(proposal)
        ? { ...record, status: "applied", resolvedAt: appliedAt }
        : record,
    ),
    audit: [
      ...updated.audit,
      {
        id: `audit-correction-applied-${appliedAt}`,
        actor,
        role: "Privacy Officer",
        action: "Applied officer-approved correction proposal",
        object: proposal.target_facts.map(({ fact_id }) => fact_id).join(", "),
        timestamp: appliedAt,
        priorValue: JSON.stringify({ instruction: proposal.instruction, proposal }),
        newValue: `Processing model v${updated.processingModel.version}; stale findings: ${proposal.stale_finding_ids.join(", ")}`,
      },
    ],
  });
}

export function updateDpiaIntake(
  caseData: DpiaCaseSnapshot,
  values: Record<string, string>,
  changedAt: string,
  actor: string,
): DpiaCaseSnapshot {
  const changedFacts = caseData.processingModel.facts.filter(
    (fact) => values[fact.id] !== undefined && values[fact.id] !== fact.value,
  );
  if (changedFacts.length === 0) return caseData;

  const materialFactIds = new Set(
    changedFacts.filter((fact) => fact.material).map((fact) => fact.id),
  );
  const nextVersion =
    materialFactIds.size > 0
      ? caseData.processingModel.version + 1
      : caseData.processingModel.version;
  const changedLabels = changedFacts.map((fact) => fact.label);
  const resolvesSignificantEffect =
    typeof values["intended-outcome"] === "string" &&
    /escalation|fitness-to-study|significant effect/i.test(values["intended-outcome"]);

  return dpiaCaseSnapshotSchema.parse({
    ...caseData,
    updatedAt: changedAt,
    officerDecision: materialFactIds.size > 0 ? undefined : caseData.officerDecision,
    processingModel: {
      ...caseData.processingModel,
      version: nextVersion,
      updatedAt: changedAt,
      facts: caseData.processingModel.facts.map((fact) =>
        values[fact.id] === undefined
          ? fact
          : {
              ...fact,
              value: values[fact.id],
              status: values[fact.id].trim() === "" ? "missing" : "confirmed",
            },
      ),
    },
    contradictions: caseData.contradictions.map((contradiction) =>
      contradiction.id === "con-significant-effect" && resolvesSignificantEffect
        ? {
            ...contradiction,
            resolved: true,
            resolution:
              "The intake now acknowledges possible escalation and fitness-to-study effects.",
          }
        : contradiction,
    ),
    determinations: caseData.determinations.map((determination) => {
      if (determination.dependencyFactIds.some((factId) => materialFactIds.has(factId))) {
        return {
          ...determination,
          status: "stale-after-change",
          staleReason: `${changedLabels.join(", ")} changed in processing model v${nextVersion}.`,
        };
      }
      return materialFactIds.size > 0 && determination.status !== "stale-after-change"
        ? { ...determination, processingModelVersion: nextVersion }
        : determination;
    }),
    audit: [
      ...caseData.audit,
      {
        id: `audit-intake-${changedAt}`,
        actor,
        role: "Privacy Officer",
        action: "Updated material intake facts",
        object: changedLabels.join(", "),
        timestamp: changedAt,
        priorValue: `Processing model v${caseData.processingModel.version}`,
        newValue: `Processing model v${nextVersion}`,
      },
    ],
  });
}

export function answerStakeholderQuestion(
  caseData: DpiaCaseSnapshot,
  input: { questionId: string; response: string; answeredBy: string; answeredAt: string },
): DpiaCaseSnapshot {
  const question = caseData.questions.find((candidate) => candidate.id === input.questionId);
  if (!question) throw new Error(`Unknown stakeholder question: ${input.questionId}`);
  if (input.response.trim().length < 10)
    throw new Error("A stakeholder answer must contain useful detail.");
  if (input.answeredBy.trim().length < 2) throw new Error("Record who supplied the answer.");

  const evidenceId = `ev-answer-${input.questionId}-${input.answeredAt}`;
  const dependentFactIds = new Set(questionFactDependencies[input.questionId] ?? []);
  const materialFactIds = new Set(
    caseData.processingModel.facts
      .filter((fact) => fact.material && dependentFactIds.has(fact.id))
      .map((fact) => fact.id),
  );
  const nextVersion =
    materialFactIds.size > 0
      ? caseData.processingModel.version + 1
      : caseData.processingModel.version;

  return dpiaCaseSnapshotSchema.parse({
    ...caseData,
    updatedAt: input.answeredAt,
    officerDecision: materialFactIds.size > 0 ? undefined : caseData.officerDecision,
    processingModel: {
      ...caseData.processingModel,
      version: nextVersion,
      updatedAt: input.answeredAt,
      facts: caseData.processingModel.facts.map((fact) =>
        dependentFactIds.has(fact.id)
          ? {
              ...fact,
              value: input.response,
              status: "confirmed",
              evidenceIds: Array.from(new Set([...fact.evidenceIds, evidenceId])),
            }
          : fact,
      ),
    },
    evidence: [
      ...caseData.evidence,
      {
        id: evidenceId,
        title: `Stakeholder answer: ${question.stakeholder}`,
        type: "Attributed response",
        filename: `answer-${input.questionId}.md`,
        source: question.stakeholder,
        owner: input.answeredBy,
        collectedAt: input.answeredAt,
        excerpt: input.response,
        supportedDimensionIds: question.blockedDimensionIds,
        status: "current",
        synthetic: true,
      },
    ],
    questions: caseData.questions.map((candidate) =>
      candidate.id === input.questionId
        ? {
            ...candidate,
            status: "answered",
            response: input.response,
            answeredBy: input.answeredBy,
            answeredAt: input.answeredAt,
          }
        : candidate,
    ),
    determinations: caseData.determinations.map((determination) => {
      if (determination.dependencyFactIds.some((factId) => materialFactIds.has(factId))) {
        return {
          ...determination,
          status: "stale-after-change",
          staleReason: `New attributed evidence changed processing model v${nextVersion}.`,
        };
      }
      return materialFactIds.size === 0 || determination.status === "stale-after-change"
        ? determination
        : { ...determination, processingModelVersion: nextVersion };
    }),
    audit: [
      ...caseData.audit,
      {
        id: `audit-answer-${input.questionId}-${input.answeredAt}`,
        actor: input.answeredBy,
        role: question.stakeholder,
        action: "Answered targeted question",
        object: question.text,
        timestamp: input.answeredAt,
        priorValue: `Processing model v${caseData.processingModel.version}`,
        newValue: `Processing model v${nextVersion}; ${input.response}`,
      },
    ],
  });
}

export function replayValidatedInvestigation(
  caseData: DpiaCaseSnapshot,
  replayedAt: string,
): DpiaCaseSnapshot {
  const currentVersion = caseData.processingModel.version;
  return dpiaCaseSnapshotSchema.parse({
    ...caseData,
    updatedAt: replayedAt,
    determinations: caseData.determinations.map((determination) => ({
      ...determination,
      processingModelVersion: currentVersion,
      status: validatedFindingStatuses[determination.id] ?? determination.status,
      staleReason: undefined,
    })),
    verification: {
      ...caseData.verification,
      reviewedAt: replayedAt,
      notes: [
        ...caseData.verification.notes,
        `Validated snapshot logic replayed against processing model v${currentVersion}; live agent output was not used.`,
      ],
    },
    agentActivity: caseData.agentActivity.map((activity) => ({
      ...activity,
      status: "completed",
      completedAt: replayedAt,
      detail: `${activity.detail} Replayed against processing model v${currentVersion}.`,
    })),
    audit: [
      ...caseData.audit,
      {
        id: `audit-validated-replay-${replayedAt}`,
        actor: "Deterministic synthesizer",
        role: "System",
        action: "Replayed validated investigation",
        object: "Structured professional-role artifacts",
        timestamp: replayedAt,
        newValue: `Validated demo snapshot rebound to processing model v${currentVersion}; no live agent run`,
      },
    ],
  });
}

export function recordOfficerDecision(
  caseData: DpiaCaseSnapshot,
  decisionCandidate: OfficerDecision,
): DpiaCaseSnapshot {
  const decision = officerDecisionSchema.parse(decisionCandidate);
  if (decision.processingModelVersion !== caseData.processingModel.version) {
    throw new Error(
      `Officer decision targets processing model v${decision.processingModelVersion}; current model is v${caseData.processingModel.version}.`,
    );
  }
  if (decision.rationale.trim().length < 10) {
    throw new Error("An officer decision requires a substantive rationale.");
  }

  const auditEvent: AuditEvent = {
    id: `audit-officer-${decision.decidedAt}`,
    actor: decision.officer,
    role: "Privacy Officer",
    action:
      decision.action === "accepted"
        ? "Accepted screening recommendation"
        : decision.action === "edited"
          ? "Edited screening determination"
          : decision.action === "rejected"
            ? "Rejected screening recommendation"
            : "Requested more information",
    object: "Screening recommendation",
    timestamp: decision.decidedAt,
    priorValue: caseData.officerDecision?.outcome ?? caseData.recommendation,
    newValue: decision.outcome,
  };

  return dpiaCaseSnapshotSchema.parse({
    ...caseData,
    stage:
      decision.outcome === "full-dpia-likely" && decision.action !== "more-information"
        ? "Full DPIA in progress"
        : "Screening review",
    recommendation: decision.outcome,
    updatedAt: decision.decidedAt,
    officerDecision: decision,
    audit: [...caseData.audit, auditEvent],
  });
}
