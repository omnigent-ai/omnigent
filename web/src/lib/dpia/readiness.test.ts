import { describe, expect, it } from "vitest";
import { evidenceItemSchema, processingModelSchema } from "./schemas";
import { calculateReadiness, changeProcessingFact } from "./readiness";
import type {
  Contradiction,
  Determination,
  EvidenceItem,
  ProcessingModel,
  ReadinessDefinition,
} from "./types";

const definitions: ReadinessDefinition[] = [
  { id: "purpose-scope", label: "Purpose and scope defined", requiredFactIds: ["purpose"] },
  {
    id: "vendor-transfer",
    label: "Vendor, processor, and transfer facts evidenced",
    requiredFactIds: ["hosting"],
  },
];

const evidence: EvidenceItem[] = [
  {
    id: "ev-intake",
    title: "Project intake form",
    type: "Intake",
    filename: "project-intake.md",
    source: "Student Services",
    owner: "Programme Director",
    collectedAt: "2026-08-10T09:00:00Z",
    excerpt: "The service predicts disengagement and recommends support interventions.",
    supportedDimensionIds: ["purpose-scope"],
    status: "current",
    synthetic: true,
  },
];

const processingModel: ProcessingModel = {
  caseId: "student-success-alert",
  version: 3,
  updatedAt: "2026-08-12T11:00:00Z",
  facts: [
    {
      id: "purpose",
      section: "Purpose and intended outcomes",
      label: "Intended purpose",
      value: "Predict disengagement and recommend support",
      material: true,
      status: "confirmed",
      evidenceIds: ["ev-intake"],
      dependentDimensionIds: ["purpose-scope"],
    },
    {
      id: "hosting",
      section: "Systems and vendors",
      label: "Model and database hosting",
      value: "",
      material: true,
      status: "missing",
      evidenceIds: [],
      dependentDimensionIds: ["vendor-transfer"],
    },
  ],
};

const determinations: Determination[] = [
  {
    id: "det-purpose",
    dimensionId: "purpose-scope",
    question: "Is the purpose and scope defined?",
    outcome: "met",
    status: "confirmed",
    reasoning: "The intake states the intended intervention purpose.",
    evidenceReferences: [
      { evidenceId: "ev-intake", excerpt: "predicts disengagement and recommends support" },
    ],
    policyReferences: [{ ruleId: "uk-gdpr-35", label: "UK GDPR Article 35" }],
    dependencyFactIds: ["purpose"],
    processingModelVersion: 3,
    reviewer: "Privacy Assessor",
    gaps: [],
  },
];

describe("DPIA contracts and readiness", () => {
  it("accepts schema-valid synthetic artifacts and rejects unlabeled evidence", () => {
    expect(processingModelSchema.parse(processingModel)).toEqual(processingModel);
    expect(evidenceItemSchema.parse(evidence[0])).toEqual(evidence[0]);
    expect(() => evidenceItemSchema.parse({ ...evidence[0], synthetic: false })).toThrow();
  });

  it("counts only evidenced dimensions without unresolved contradictions as answerable", () => {
    const result = calculateReadiness(processingModel, evidence, [], definitions);

    expect(result.answerable).toBe(1);
    expect(result.total).toBe(2);
    expect(result.dimensions.map(({ id, status }) => ({ id, status }))).toEqual([
      { id: "purpose-scope", status: "answerable" },
      { id: "vendor-transfer", status: "missing-evidence" },
    ]);
  });

  it("blocks an otherwise evidenced dimension when sources materially conflict", () => {
    const contradictions: Contradiction[] = [
      {
        id: "con-purpose",
        title: "Support-only claim conflicts with escalation procedure",
        summary: "The operating procedure permits consequential escalation.",
        dimensionIds: ["purpose-scope"],
        sourceReferences: [
          { evidenceId: "ev-intake", excerpt: "support only" },
          { evidenceId: "ev-procedure", excerpt: "attendance escalation" },
        ],
        material: true,
        resolved: false,
      },
    ];

    const result = calculateReadiness(processingModel, evidence, contradictions, definitions);

    expect(result.dimensions[0].status).toBe("needs-judgement");
    expect(result.answerable).toBe(0);
  });

  it("increments the model version and invalidates only dependent determinations", () => {
    const unrelatedDetermination: Determination = {
      ...determinations[0],
      id: "det-vendor",
      dimensionId: "vendor-transfer",
      dependencyFactIds: ["hosting"],
    };
    const result = changeProcessingFact(
      processingModel,
      [...determinations, unrelatedDetermination],
      {
        factId: "purpose",
        value: "Persistent high-risk scores may trigger attendance escalation",
        changedAt: "2026-08-21T10:00:00Z",
      },
    );

    expect(result.changed).toBe(true);
    expect(result.processingModel.version).toBe(4);
    expect(result.determinations[0]).toMatchObject({
      status: "stale-after-change",
      processingModelVersion: 3,
      staleReason: "Intended purpose changed in processing model v4.",
    });
    expect(result.determinations[1]).toMatchObject({
      status: "confirmed",
      processingModelVersion: 4,
    });
    expect(
      calculateReadiness(result.processingModel, evidence, [], definitions, result.determinations)
        .dimensions[0].status,
    ).toBe("stale-after-change");
  });

  it("does not create a version when the persisted value is unchanged", () => {
    const result = changeProcessingFact(processingModel, determinations, {
      factId: "purpose",
      value: processingModel.facts[0].value,
      changedAt: "2026-08-21T10:00:00Z",
    });

    expect(result).toEqual({ processingModel, determinations, changed: false });
  });
});
