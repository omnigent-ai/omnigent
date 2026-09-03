import { correctionProposalSchema } from "./schemas";
import type { CorrectionProposal, DpiaCaseSnapshot } from "./types";
import { toCaseEvidenceId } from "./agentContext";

export interface ManualCorrectionInput {
  factId: string;
  proposedValue: string;
  evidenceId: string;
  instruction: string;
  rationale: string;
  roleToReassess?: CorrectionProposal["role_to_reassess"];
}

function sameValues(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

export function parseCorrectionProposalText(text: string): CorrectionProposal | null {
  try {
    const proposal = correctionProposalSchema.parse(JSON.parse(text.trim())) as CorrectionProposal;
    return {
      ...proposal,
      new_evidence_refs: proposal.new_evidence_refs.map((reference) => ({
        ...reference,
        evidence_id: toCaseEvidenceId(reference.evidence_id),
      })),
    };
  } catch {
    return null;
  }
}

export function staleFindingIdsForFacts(
  caseData: DpiaCaseSnapshot,
  factIds: ReadonlySet<string>,
): string[] {
  return caseData.determinations
    .filter((finding) => finding.dependencyFactIds.some((factId) => factIds.has(factId)))
    .map(({ id }) => id)
    .sort();
}

export function validateCorrectionProposal(
  caseData: DpiaCaseSnapshot,
  candidate: unknown,
): CorrectionProposal {
  const proposal = correctionProposalSchema.parse(candidate) as CorrectionProposal;
  if (proposal.case_id !== caseData.id) throw new Error("The proposal targets another DPIA case.");
  if (proposal.processing_model_version !== caseData.processingModel.version) {
    throw new Error(
      `The proposal targets processing model v${proposal.processing_model_version}; current model is v${caseData.processingModel.version}.`,
    );
  }
  if (proposal.policy_pack_version !== caseData.policyPack.version) {
    throw new Error(
      `The proposal targets policy ${proposal.policy_pack_version}; current policy is ${caseData.policyPack.version}.`,
    );
  }

  const facts = new Map(caseData.processingModel.facts.map((fact) => [fact.id, fact]));
  const changedMaterialFactIds = new Set<string>();
  for (const target of proposal.target_facts) {
    const fact = facts.get(target.fact_id);
    if (!fact) throw new Error(`Unknown correction fact: ${target.fact_id}`);
    if (target.current_value !== fact.value) {
      throw new Error(`The current value for ${fact.label} has changed since this proposal.`);
    }
    if (target.proposed_value === fact.value) {
      throw new Error(`The proposed value for ${fact.label} does not change the case.`);
    }
    if (fact.material) changedMaterialFactIds.add(fact.id);
  }
  if (changedMaterialFactIds.size === 0) {
    throw new Error("A correction proposal must change at least one material fact.");
  }

  const evidenceIds = new Set(caseData.evidence.map(({ id }) => id));
  for (const reference of proposal.new_evidence_refs) {
    if (!evidenceIds.has(reference.evidence_id)) {
      throw new Error(`Unknown correction evidence: ${reference.evidence_id}`);
    }
  }
  const findingIds = new Set(caseData.determinations.map(({ id }) => id));
  for (const findingId of proposal.affected_finding_ids) {
    if (!findingIds.has(findingId)) throw new Error(`Unknown affected finding: ${findingId}`);
  }
  for (const findingId of proposal.stale_finding_ids) {
    if (!findingIds.has(findingId)) throw new Error(`Unknown stale finding: ${findingId}`);
  }

  const expectedStaleIds = staleFindingIdsForFacts(caseData, changedMaterialFactIds);
  const proposedStaleIds = [...proposal.stale_finding_ids].sort();
  if (!sameValues(proposedStaleIds, expectedStaleIds)) {
    throw new Error(
      `The proposal stale set does not match the changed facts. Expected: ${expectedStaleIds.join(", ")}.`,
    );
  }
  return proposal;
}

export function buildManualCorrectionProposal(
  caseData: DpiaCaseSnapshot,
  input: ManualCorrectionInput,
): CorrectionProposal {
  const fact = caseData.processingModel.facts.find(({ id }) => id === input.factId);
  if (!fact) throw new Error(`Unknown correction fact: ${input.factId}`);
  const evidence = caseData.evidence.find(({ id }) => id === input.evidenceId);
  if (!evidence) throw new Error(`Unknown correction evidence: ${input.evidenceId}`);
  const staleFindingIds = staleFindingIdsForFacts(caseData, new Set([fact.id]));
  return validateCorrectionProposal(caseData, {
    artifact: "correction-proposal",
    case_id: caseData.id,
    processing_model_version: caseData.processingModel.version,
    policy_pack_version: caseData.policyPack.version,
    instruction: input.instruction,
    target_facts: [
      {
        fact_id: fact.id,
        current_value: fact.value,
        proposed_value: input.proposedValue,
      },
    ],
    new_evidence_refs: [{ evidence_id: evidence.id, excerpt: evidence.excerpt }],
    affected_finding_ids: staleFindingIds,
    expected_version_bump: {
      from: caseData.processingModel.version,
      to: caseData.processingModel.version + 1,
    },
    stale_finding_ids: staleFindingIds,
    role_to_reassess: input.roleToReassess ?? "privacy_assessor",
    rationale: input.rationale,
  });
}
