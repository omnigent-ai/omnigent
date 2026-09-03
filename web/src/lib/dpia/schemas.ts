import { z } from "zod";
import { lifecycleStages, readinessDimensionIds } from "./types";

const lifecycleStageSchema = z.enum(lifecycleStages);
const readinessDimensionIdSchema = z.enum(readinessDimensionIds);
const evidenceReferenceSchema = z
  .object({ evidenceId: z.string().min(1), excerpt: z.string().min(1) })
  .strict();
const policyReferenceSchema = z
  .object({ ruleId: z.string().min(1), label: z.string().min(1) })
  .strict();

export const evidenceItemSchema = z
  .object({
    id: z.string().min(1),
    title: z.string().min(1),
    type: z.string().min(1),
    filename: z.string().min(1),
    source: z.string().min(1),
    owner: z.string().min(1),
    collectedAt: z.string().min(1),
    excerpt: z.string().min(1),
    supportedDimensionIds: z.array(readinessDimensionIdSchema),
    status: z.enum(["current", "stale", "expired"]),
    synthetic: z.literal(true),
  })
  .strict();

export const processingFactSchema = z
  .object({
    id: z.string().min(1),
    section: z.string().min(1),
    label: z.string().min(1),
    value: z.string(),
    lifecycleStage: lifecycleStageSchema.optional(),
    material: z.boolean(),
    status: z.enum(["confirmed", "missing", "stale"]),
    evidenceIds: z.array(z.string().min(1)),
    dependentDimensionIds: z.array(readinessDimensionIdSchema),
  })
  .strict();

export const processingModelSchema = z
  .object({
    caseId: z.string().min(1),
    version: z.number().int().positive(),
    updatedAt: z.string().min(1),
    facts: z.array(processingFactSchema).min(1),
  })
  .strict();

export const lifecycleNodeSchema = z
  .object({
    stage: lifecycleStageSchema,
    data: z.array(z.string().min(1)),
    purpose: z.string().min(1),
    actors: z.array(z.string().min(1)),
    systems: z.array(z.string().min(1)),
    location: z.string().min(1),
    recipients: z.array(z.string().min(1)),
    legalBasis: z.string().min(1),
    article9Condition: z.string().min(1).optional(),
    retention: z.string().min(1),
    controls: z.array(z.string().min(1)),
    missingFacts: z.array(z.string().min(1)),
  })
  .strict();

export const contradictionSchema = z
  .object({
    id: z.string().min(1),
    title: z.string().min(1),
    summary: z.string().min(1),
    dimensionIds: z.array(readinessDimensionIdSchema).min(1),
    sourceReferences: z.array(evidenceReferenceSchema).min(2),
    material: z.boolean(),
    resolved: z.boolean(),
    resolution: z.string().min(1).optional(),
  })
  .strict();

export const stakeholderQuestionSchema = z
  .object({
    id: z.string().min(1),
    stakeholder: z.string().min(1),
    text: z.string().min(1),
    blockedDimensionIds: z.array(readinessDimensionIdSchema).min(1),
    status: z.enum(["draft", "approved", "answered", "unanswered"]),
    response: z.string().min(1).optional(),
    answeredBy: z.string().min(1).optional(),
    answeredAt: z.string().min(1).optional(),
  })
  .strict();

export const determinationSchema = z
  .object({
    id: z.string().min(1),
    dimensionId: readinessDimensionIdSchema,
    question: z.string().min(1),
    outcome: z.enum(["met", "not-met", "unclear"]),
    status: z.enum([
      "confirmed",
      "needs-judgement",
      "potential-issue",
      "missing-evidence",
      "stale-after-change",
    ]),
    reasoning: z.string().min(1),
    evidenceReferences: z.array(evidenceReferenceSchema),
    policyReferences: z.array(policyReferenceSchema),
    dependencyFactIds: z.array(z.string().min(1)),
    processingModelVersion: z.number().int().positive(),
    reviewer: z.string().min(1),
    dissent: z.string().min(1).optional(),
    gaps: z.array(z.string().min(1)),
    staleReason: z.string().min(1).optional(),
  })
  .strict();

export const riskItemSchema = z
  .object({
    id: z.string().min(1),
    harm: z.string().min(1),
    affectedSubjects: z.string().min(1),
    likelihood: z.enum(["low", "medium", "high"]),
    severity: z.enum(["low", "medium", "high"]),
    inherentRating: z.enum(["low", "medium", "high", "critical"]),
    controls: z.array(z.string().min(1)),
    mitigation: z.string().min(1),
    residualRating: z.enum(["low", "medium", "high", "critical"]),
    owner: z.string().min(1),
    dueDate: z.string().min(1),
  })
  .strict();

export const verificationSchema = z
  .object({
    verdict: z.enum(["verified", "verified-with-caveats", "failed"]),
    reviewedAt: z.string().min(1),
    reviewer: z.string().min(1),
    blindedUntil: z.string().min(1),
    citationCoverage: z.string().min(1),
    unsupportedClaimIds: z.array(z.string().min(1)),
    notes: z.array(z.string().min(1)),
  })
  .strict();

export const officerDecisionSchema = z
  .object({
    action: z.enum(["accepted", "edited", "rejected", "more-information"]),
    outcome: z.enum(["full-dpia-likely", "no-full-dpia-indicated", "more-information-required"]),
    rationale: z.string().min(1),
    officer: z.string().min(1),
    decidedAt: z.string().min(1),
    processingModelVersion: z.number().int().positive(),
    policyPackVersion: z.string().min(1),
  })
  .strict();

export const auditEventSchema = z
  .object({
    id: z.string().min(1),
    actor: z.string().min(1),
    role: z.string().min(1),
    action: z.string().min(1),
    object: z.string().min(1),
    timestamp: z.string().min(1),
    priorValue: z.string().optional(),
    newValue: z.string().optional(),
  })
  .strict();

export const agentActivitySchema = z
  .object({
    id: z.string().min(1),
    role: z.enum(["Process Investigator", "Privacy Assessor", "Independent Verifier"]),
    task: z.string().min(1),
    status: z.enum(["queued", "running", "completed", "failed"]),
    startedAt: z.string().min(1).optional(),
    completedAt: z.string().min(1).optional(),
    detail: z.string().min(1),
  })
  .strict();

export const policyPackSchema = z
  .object({
    jurisdiction: z.literal("UK"),
    version: z.string().min(1),
    effectiveDate: z.string().min(1),
    rules: z
      .array(
        z
          .object({
            id: z.string().min(1),
            title: z.string().min(1),
            source: z.string().min(1),
            guidance: z.string().min(1),
          })
          .strict(),
      )
      .min(1),
  })
  .strict();

export const correctionProposalSchema = z
  .object({
    artifact: z.literal("correction-proposal"),
    case_id: z.literal("student-success-alert"),
    processing_model_version: z.number().int().positive(),
    policy_pack_version: z.string().min(1),
    instruction: z.string().min(1),
    target_facts: z
      .array(
        z
          .object({
            fact_id: z.string().min(1),
            current_value: z.string().nullable(),
            proposed_value: z.string().min(1),
          })
          .strict(),
      )
      .min(1),
    new_evidence_refs: z.array(
      z
        .object({
          evidence_id: z.string().min(1),
          excerpt: z.string().min(1),
        })
        .strict(),
    ),
    affected_finding_ids: z.array(z.string().min(1)).min(1),
    expected_version_bump: z
      .object({
        from: z.number().int().positive(),
        to: z.number().int().min(2),
      })
      .strict(),
    stale_finding_ids: z.array(z.string().min(1)),
    role_to_reassess: z.enum(["process_investigator", "privacy_assessor", "independent_verifier"]),
    rationale: z.string().min(10),
  })
  .strict()
  .superRefine((proposal, context) => {
    const targetFactIds = proposal.target_facts.map(({ fact_id }) => fact_id);
    if (new Set(targetFactIds).size !== targetFactIds.length) {
      context.addIssue({
        code: "custom",
        path: ["target_facts"],
        message: "Target fact ids must be unique.",
      });
    }
    if (new Set(proposal.affected_finding_ids).size !== proposal.affected_finding_ids.length) {
      context.addIssue({
        code: "custom",
        path: ["affected_finding_ids"],
        message: "Affected finding ids must be unique.",
      });
    }
    if (new Set(proposal.stale_finding_ids).size !== proposal.stale_finding_ids.length) {
      context.addIssue({
        code: "custom",
        path: ["stale_finding_ids"],
        message: "Stale finding ids must be unique.",
      });
    }
    if (proposal.expected_version_bump.from !== proposal.processing_model_version) {
      context.addIssue({
        code: "custom",
        path: ["expected_version_bump", "from"],
        message: "Expected version must start at the proposal processing-model version.",
      });
    }
    if (proposal.expected_version_bump.to !== proposal.processing_model_version + 1) {
      context.addIssue({
        code: "custom",
        path: ["expected_version_bump", "to"],
        message: "Expected version must advance by exactly one.",
      });
    }
    const affectedFindingIds = new Set(proposal.affected_finding_ids);
    if (proposal.stale_finding_ids.some((findingId) => !affectedFindingIds.has(findingId))) {
      context.addIssue({
        code: "custom",
        path: ["stale_finding_ids"],
        message: "Stale findings must be included in affected findings.",
      });
    }
  });

export const correctionProposalRecordSchema = z
  .object({
    id: z.string().min(1),
    proposal: correctionProposalSchema,
    source: z.enum(["agent", "manual"]),
    status: z.enum(["pending", "applied", "rejected"]),
    createdAt: z.string().min(1),
    resolvedAt: z.string().min(1).optional(),
  })
  .strict();

export const dpiaLiveRunStateSchema = z.discriminatedUnion("status", [
  z
    .object({
      status: z.literal("failed"),
      message: z.string().min(1),
      updatedAt: z.string().min(1),
    })
    .strict(),
  z
    .object({
      status: z.literal("completed"),
      message: z.string().min(1),
      sessionId: z.string().min(1),
      updatedAt: z.string().min(1),
    })
    .strict(),
]);

export const dpiaCaseSnapshotSchema = z
  .object({
    id: z.string().min(1),
    sessionId: z.string().min(1).optional(),
    title: z.string().min(1),
    owner: z.string().min(1),
    jurisdiction: z.literal("UK"),
    stage: z.string().min(1),
    recommendation: z.enum([
      "full-dpia-likely",
      "no-full-dpia-indicated",
      "more-information-required",
    ]),
    snapshotLabel: z.literal("Validated demo snapshot"),
    updatedAt: z.string().min(1),
    processingModel: processingModelSchema,
    lifecycle: z.array(lifecycleNodeSchema).length(8),
    evidence: z.array(evidenceItemSchema).min(1),
    contradictions: z.array(contradictionSchema),
    questions: z.array(stakeholderQuestionSchema),
    determinations: z.array(determinationSchema),
    risks: z.array(riskItemSchema),
    verification: verificationSchema,
    officerDecision: officerDecisionSchema.optional(),
    correctionProposals: z.array(correctionProposalRecordSchema).optional(),
    liveRun: dpiaLiveRunStateSchema.optional(),
    audit: z.array(auditEventSchema),
    agentActivity: z.array(agentActivitySchema),
    policyPack: policyPackSchema,
  })
  .strict();

export const decisionPackSchema = z
  .object({
    caseId: z.string().min(1),
    generatedAt: z.string().min(1),
    processingModelVersion: z.number().int().positive(),
    policyPackVersion: z.string().min(1),
    recommendation: z.enum([
      "full-dpia-likely",
      "no-full-dpia-indicated",
      "more-information-required",
    ]),
    processingModel: processingModelSchema,
    lifecycle: z.array(lifecycleNodeSchema).length(8),
    evidence: z.array(evidenceItemSchema).min(1),
    determinations: z.array(determinationSchema).min(1),
    contradictions: z.array(contradictionSchema),
    risks: z.array(riskItemSchema).min(1),
    verification: verificationSchema,
    officerDecision: officerDecisionSchema,
    audit: z.array(auditEventSchema),
  })
  .strict();
