import type { DpiaCaseSnapshot } from "./types";

const BUNDLE_EVIDENCE_IDS = {
  "ev-intake": "EV-01",
  "ev-data-dictionary": "EV-02",
  "ev-operating-procedure": "EV-03",
  "ev-privacy-notice": "EV-04",
  "ev-retention-schedule": "EV-05",
  "ev-vendor-dpa": "EV-06",
  "ev-model-card": "EV-07",
  "ev-security-questionnaire": "EV-08",
  "ev-subprocessor-list": "EV-09",
  "ev-response-student-services": "EV-10",
  "ev-response-registry": "EV-11",
  "ev-response-security": "EV-12",
  "ev-response-disability": "EV-13",
  "ev-response-schools": "EV-14",
  "ev-response-procurement": "EV-15",
} as const;

const CASE_EVIDENCE_IDS: ReadonlyMap<string, string> = new Map(
  Object.entries(BUNDLE_EVIDENCE_IDS).map(([caseId, bundleId]) => [bundleId, caseId]),
);

export function toBundleEvidenceId(caseEvidenceId: string): string {
  return BUNDLE_EVIDENCE_IDS[caseEvidenceId as keyof typeof BUNDLE_EVIDENCE_IDS] ?? caseEvidenceId;
}

export function toCaseEvidenceId(bundleEvidenceId: string): string {
  return CASE_EVIDENCE_IDS.get(bundleEvidenceId) ?? bundleEvidenceId;
}

export function buildDpiaProcessingModelArtifact(caseData: DpiaCaseSnapshot) {
  const version = caseData.processingModel.version;
  return {
    artifact: "processing-model" as const,
    case_id: caseData.id,
    processing_model_version: version,
    evidence: caseData.evidence.map((item) => ({
      id: toBundleEvidenceId(item.id),
      title: item.title,
      source: item.source,
      owner: item.owner,
      collected_at: item.collectedAt,
      excerpt: item.excerpt,
      synthetic: item.synthetic,
    })),
    facts: caseData.processingModel.facts.map((fact) => ({
      id: fact.id,
      field: fact.label,
      value: fact.value,
      status: fact.status === "stale" ? ("unclear" as const) : fact.status,
      lifecycle_stage: fact.lifecycleStage ?? null,
      evidence_ids: fact.evidenceIds.map(toBundleEvidenceId),
      dependent_dimension_ids: fact.dependentDimensionIds,
      processing_model_version: version,
    })),
    lifecycle: caseData.lifecycle.map((node) => ({
      stage: node.stage,
      data: node.data,
      purpose: node.purpose,
      actors: node.actors,
      systems: node.systems,
      location: node.location,
      recipients: node.recipients,
      legal_basis: node.legalBasis,
      article_9_condition: node.article9Condition ?? null,
      retention: node.retention,
      controls: node.controls,
      missing_facts: node.missingFacts,
    })),
    evidence_gaps: caseData.processingModel.facts
      .filter((fact) => fact.status !== "confirmed" || fact.value.trim() === "")
      .map((fact) => ({
        id: `gap-${fact.id}`,
        fact: fact.label,
        blocked_dimension_ids: fact.dependentDimensionIds,
        owner: caseData.owner,
        material: fact.material,
      })),
    contradictions: caseData.contradictions.map((contradiction) => ({
      id: contradiction.id,
      summary: contradiction.summary,
      source_evidence_ids: contradiction.sourceReferences.map(({ evidenceId }) =>
        toBundleEvidenceId(evidenceId),
      ),
      dimension_ids: contradiction.dimensionIds,
      resolved: contradiction.resolved,
      resolution: contradiction.resolution ?? null,
    })),
    stakeholder_questions: caseData.questions.map((question) => ({
      id: question.id,
      stakeholder: question.stakeholder,
      text: question.text,
      blocked_dimension_ids: question.blockedDimensionIds,
      status: question.status,
      response: question.response ?? null,
      answered_by: question.answeredBy ?? null,
      answered_at: question.answeredAt ?? null,
    })),
  };
}

export function buildDpiaAgentMessage(caseData: DpiaCaseSnapshot, officerMessage: string): string {
  return [
    "Officer message follows. Preserve it verbatim when delegating a correction instruction:",
    officerMessage,
    `Processing model version: ${caseData.processingModel.version}`,
    `Policy pack version: ${caseData.policyPack.version}`,
    "Current processing model JSON follows. Treat it as authoritative for current facts and version; use the bundled evidence pack only for cited provenance:",
    JSON.stringify(buildDpiaProcessingModelArtifact(caseData)),
    "Current determination dependency map JSON follows. Use only these finding IDs for affected_finding_ids and stale_finding_ids:",
    JSON.stringify(
      caseData.determinations.map((finding) => ({
        finding_id: finding.id,
        dependency_fact_ids: finding.dependencyFactIds,
      })),
    ),
  ].join("\n");
}

export function officerMessageFromAgentMessage(message: string): string {
  const prefix =
    "Officer message follows. Preserve it verbatim when delegating a correction instruction:\n";
  const suffix = "\nProcessing model version:";
  if (!message.startsWith(prefix)) return message;
  const suffixIndex = message.indexOf(suffix, prefix.length);
  return suffixIndex === -1 ? message : message.slice(prefix.length, suffixIndex).trim();
}
