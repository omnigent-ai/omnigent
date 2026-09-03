export const lifecycleStages = [
  "collection",
  "storage",
  "access",
  "use",
  "sharing",
  "transfer",
  "retention",
  "deletion",
] as const;

export type LifecycleStage = (typeof lifecycleStages)[number];

export const readinessDimensionIds = [
  "purpose-scope",
  "lifecycle-flows",
  "legal-basis",
  "necessity-proportionality",
  "student-harms-rights",
  "vendor-transfer",
  "security-controls",
  "transparency-retention",
] as const;

export type ReadinessDimensionId = (typeof readinessDimensionIds)[number];

export type ReadinessStatus =
  "answerable" | "needs-judgement" | "missing-evidence" | "stale-after-change";

export type FindingStatus =
  "confirmed" | "needs-judgement" | "potential-issue" | "missing-evidence" | "stale-after-change";

export interface EvidenceReference {
  evidenceId: string;
  excerpt: string;
}

export interface PolicyReference {
  ruleId: string;
  label: string;
}

export interface EvidenceItem {
  id: string;
  title: string;
  type: string;
  filename: string;
  source: string;
  owner: string;
  collectedAt: string;
  excerpt: string;
  supportedDimensionIds: ReadinessDimensionId[];
  status: "current" | "stale" | "expired";
  synthetic: true;
}

export interface ProcessingFact {
  id: string;
  section: string;
  label: string;
  value: string;
  lifecycleStage?: LifecycleStage;
  material: boolean;
  status: "confirmed" | "missing" | "stale";
  evidenceIds: string[];
  dependentDimensionIds: ReadinessDimensionId[];
}

export interface ProcessingModel {
  caseId: string;
  version: number;
  updatedAt: string;
  facts: ProcessingFact[];
}

export interface LifecycleNode {
  stage: LifecycleStage;
  data: string[];
  purpose: string;
  actors: string[];
  systems: string[];
  location: string;
  recipients: string[];
  legalBasis: string;
  article9Condition?: string;
  retention: string;
  controls: string[];
  missingFacts: string[];
}

export interface ReadinessDefinition {
  id: ReadinessDimensionId;
  label: string;
  requiredFactIds: string[];
}

export interface ReadinessDimension extends ReadinessDefinition {
  status: ReadinessStatus;
  detail: string;
  blockingFactIds: string[];
  evidenceIds: string[];
}

export interface ReadinessSummary {
  answerable: number;
  total: number;
  dimensions: ReadinessDimension[];
}

export interface Contradiction {
  id: string;
  title: string;
  summary: string;
  dimensionIds: ReadinessDimensionId[];
  sourceReferences: EvidenceReference[];
  material: boolean;
  resolved: boolean;
  resolution?: string;
}

export interface StakeholderQuestion {
  id: string;
  stakeholder: string;
  text: string;
  blockedDimensionIds: ReadinessDimensionId[];
  status: "draft" | "approved" | "answered" | "unanswered";
  response?: string;
  answeredBy?: string;
  answeredAt?: string;
}

export interface Determination {
  id: string;
  dimensionId: ReadinessDimensionId;
  question: string;
  outcome: "met" | "not-met" | "unclear";
  status: FindingStatus;
  reasoning: string;
  evidenceReferences: EvidenceReference[];
  policyReferences: PolicyReference[];
  dependencyFactIds: string[];
  processingModelVersion: number;
  reviewer: string;
  dissent?: string;
  gaps: string[];
  staleReason?: string;
}

export interface RiskItem {
  id: string;
  harm: string;
  affectedSubjects: string;
  likelihood: "low" | "medium" | "high";
  severity: "low" | "medium" | "high";
  inherentRating: "low" | "medium" | "high" | "critical";
  controls: string[];
  mitigation: string;
  residualRating: "low" | "medium" | "high" | "critical";
  owner: string;
  dueDate: string;
}

export interface Verification {
  verdict: "verified" | "verified-with-caveats" | "failed";
  reviewedAt: string;
  reviewer: string;
  blindedUntil: string;
  citationCoverage: string;
  unsupportedClaimIds: string[];
  notes: string[];
}

export interface OfficerDecision {
  action: "accepted" | "edited" | "rejected" | "more-information";
  outcome: "full-dpia-likely" | "no-full-dpia-indicated" | "more-information-required";
  rationale: string;
  officer: string;
  decidedAt: string;
  processingModelVersion: number;
  policyPackVersion: string;
}

export interface AuditEvent {
  id: string;
  actor: string;
  role: string;
  action: string;
  object: string;
  timestamp: string;
  priorValue?: string;
  newValue?: string;
}

export interface AgentActivity {
  id: string;
  role: "Process Investigator" | "Privacy Assessor" | "Independent Verifier";
  task: string;
  status: "queued" | "running" | "completed" | "failed";
  startedAt?: string;
  completedAt?: string;
  detail: string;
}

export interface CorrectionProposal {
  artifact: "correction-proposal";
  case_id: "student-success-alert";
  processing_model_version: number;
  policy_pack_version: string;
  instruction: string;
  target_facts: {
    fact_id: string;
    current_value: string | null;
    proposed_value: string;
  }[];
  new_evidence_refs: { evidence_id: string; excerpt: string }[];
  affected_finding_ids: string[];
  expected_version_bump: { from: number; to: number };
  stale_finding_ids: string[];
  role_to_reassess: "process_investigator" | "privacy_assessor" | "independent_verifier";
  rationale: string;
}

export interface CorrectionProposalRecord {
  id: string;
  proposal: CorrectionProposal;
  source: "agent" | "manual";
  status: "pending" | "applied" | "rejected";
  createdAt: string;
  resolvedAt?: string;
}

export type DpiaLiveRunState =
  | { status: "failed"; message: string; updatedAt: string }
  | { status: "completed"; message: string; sessionId: string; updatedAt: string };

export interface PolicyRule {
  id: string;
  title: string;
  source: string;
  guidance: string;
}

export interface PolicyPack {
  jurisdiction: "UK";
  version: string;
  effectiveDate: string;
  rules: PolicyRule[];
}

export interface DpiaCaseSnapshot {
  id: string;
  sessionId?: string;
  title: string;
  owner: string;
  jurisdiction: "UK";
  stage: string;
  recommendation: "full-dpia-likely" | "no-full-dpia-indicated" | "more-information-required";
  snapshotLabel: "Validated demo snapshot";
  updatedAt: string;
  processingModel: ProcessingModel;
  lifecycle: LifecycleNode[];
  evidence: EvidenceItem[];
  contradictions: Contradiction[];
  questions: StakeholderQuestion[];
  determinations: Determination[];
  risks: RiskItem[];
  verification: Verification;
  officerDecision?: OfficerDecision;
  correctionProposals?: CorrectionProposalRecord[];
  liveRun?: DpiaLiveRunState;
  audit: AuditEvent[];
  agentActivity: AgentActivity[];
  policyPack: PolicyPack;
}

export interface DecisionPack {
  caseId: string;
  generatedAt: string;
  processingModelVersion: number;
  policyPackVersion: string;
  recommendation: DpiaCaseSnapshot["recommendation"];
  processingModel: ProcessingModel;
  lifecycle: LifecycleNode[];
  evidence: EvidenceItem[];
  determinations: Determination[];
  contradictions: Contradiction[];
  risks: RiskItem[];
  verification: Verification;
  officerDecision: OfficerDecision;
  audit: AuditEvent[];
}
