import { decisionPackSchema, dpiaCaseSnapshotSchema } from "./schemas";
import type { DecisionPack, DpiaCaseSnapshot } from "./types";

export type SynthesisRefusalCode =
  | "invalid-artifact"
  | "missing-officer-decision"
  | "cross-version-input"
  | "stale-input"
  | "unsupported-finding"
  | "failed-verification";

export class SynthesisRefusal extends Error {
  readonly code: SynthesisRefusalCode;

  constructor(code: SynthesisRefusalCode, message: string) {
    super(message);
    this.name = "SynthesisRefusal";
    this.code = code;
  }
}

export function synthesizeDecisionPack(
  candidate: DpiaCaseSnapshot,
  generatedAt: string,
): DecisionPack {
  const parsed = dpiaCaseSnapshotSchema.safeParse(candidate);
  if (!parsed.success) {
    throw new SynthesisRefusal(
      "invalid-artifact",
      "The case contains an invalid structured artifact.",
    );
  }

  const snapshot = parsed.data;
  if (!snapshot.officerDecision) {
    throw new SynthesisRefusal(
      "missing-officer-decision",
      "A Privacy Officer decision is required before the decision pack can be assembled.",
    );
  }
  if (snapshot.verification.verdict === "failed") {
    throw new SynthesisRefusal(
      "failed-verification",
      "Independent verification failed; resolve the verifier findings before synthesis.",
    );
  }

  const expectedVersion = snapshot.processingModel.version;
  const mismatchedDeterminations = snapshot.determinations.filter(
    (determination) => determination.processingModelVersion !== expectedVersion,
  );
  if (
    mismatchedDeterminations.length > 0 ||
    snapshot.officerDecision.processingModelVersion !== expectedVersion
  ) {
    throw new SynthesisRefusal(
      "cross-version-input",
      `All inputs must target processing model v${expectedVersion}.`,
    );
  }

  const staleDeterminations = snapshot.determinations.filter(
    (determination) => determination.status === "stale-after-change",
  );
  if (staleDeterminations.length > 0) {
    throw new SynthesisRefusal(
      "stale-input",
      `Reassess ${staleDeterminations.length} stale determination${staleDeterminations.length === 1 ? "" : "s"} before synthesis.`,
    );
  }

  const evidenceIds = new Set(snapshot.evidence.map((item) => item.id));
  const policyRuleIds = new Set(snapshot.policyPack.rules.map((rule) => rule.id));
  const unsupported = snapshot.determinations.filter((determination) => {
    if (determination.status === "missing-evidence") return determination.gaps.length === 0;
    return (
      determination.evidenceReferences.length === 0 ||
      determination.policyReferences.length === 0 ||
      determination.evidenceReferences.some(
        (reference) => !evidenceIds.has(reference.evidenceId),
      ) ||
      determination.policyReferences.some((reference) => !policyRuleIds.has(reference.ruleId))
    );
  });
  if (unsupported.length > 0) {
    throw new SynthesisRefusal(
      "unsupported-finding",
      `Material findings are unsupported: ${unsupported.map((finding) => finding.id).join(", ")}.`,
    );
  }

  return decisionPackSchema.parse({
    caseId: snapshot.id,
    generatedAt,
    processingModelVersion: expectedVersion,
    policyPackVersion: snapshot.policyPack.version,
    recommendation: snapshot.recommendation,
    processingModel: snapshot.processingModel,
    lifecycle: snapshot.lifecycle,
    evidence: snapshot.evidence,
    determinations: snapshot.determinations,
    contradictions: snapshot.contradictions,
    risks: snapshot.risks,
    verification: snapshot.verification,
    officerDecision: snapshot.officerDecision,
    audit: snapshot.audit,
  });
}
