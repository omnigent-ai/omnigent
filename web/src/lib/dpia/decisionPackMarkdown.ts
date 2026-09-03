import type { DecisionPack } from "./types";

export function decisionPackToMarkdown(pack: DecisionPack): string {
  const lines = [
    `# DPIA Decision Pack: ${pack.processingModel.caseId}`,
    "",
    `Generated: ${pack.generatedAt}`,
    `Processing model: v${pack.processingModelVersion}`,
    `UK policy pack: ${pack.policyPackVersion}`,
    `Screening outcome: ${pack.recommendation.replaceAll("-", " ")}`,
    "",
    "All people, organisations, documents, and responses in this pack are synthetic.",
    "",
    "## Privacy Officer decision",
    "",
    `- Officer: ${pack.officerDecision.officer}`,
    `- Action: ${pack.officerDecision.action}`,
    `- Outcome: ${pack.officerDecision.outcome.replaceAll("-", " ")}`,
    `- Decided: ${pack.officerDecision.decidedAt}`,
    `- Rationale: ${pack.officerDecision.rationale}`,
    "",
    "## Processing model",
    "",
    ...pack.processingModel.facts.flatMap((fact) => [
      `### ${fact.label}`,
      "",
      fact.value || "Unsupported / missing evidence",
      "",
      `Status: ${fact.status}; evidence: ${fact.evidenceIds.join(", ") || "none"}`,
      "",
    ]),
    "## Processing lifecycle",
    "",
    ...pack.lifecycle.flatMap((node) => [
      `### ${titleCase(node.stage)}`,
      "",
      `- Data: ${node.data.join(", ")}`,
      `- Purpose: ${node.purpose}`,
      `- Actors: ${node.actors.join(", ")}`,
      `- Systems: ${node.systems.join(", ")}`,
      `- Location: ${node.location}`,
      `- Legal basis: ${node.legalBasis}`,
      `- Retention: ${node.retention}`,
      `- Missing facts: ${node.missingFacts.join("; ") || "none"}`,
      "",
    ]),
    "## Evidence ledger",
    "",
    "| Evidence | Source / owner | Collected | Excerpt | Status |",
    "|---|---|---|---|---|",
    ...pack.evidence.map(
      (item) =>
        `| ${escapeCell(item.title)} | ${escapeCell(`${item.source} / ${item.owner}`)} | ${item.collectedAt} | ${escapeCell(item.excerpt)} | ${item.status} |`,
    ),
    "",
    "## Screening determinations",
    "",
    ...pack.determinations.flatMap((finding) => [
      `### ${finding.question}`,
      "",
      `Outcome: ${finding.outcome}; status: ${finding.status}; reviewer: ${finding.reviewer}`,
      "",
      finding.reasoning,
      "",
      `Evidence: ${finding.evidenceReferences.map((reference) => reference.evidenceId).join(", ") || "unsupported"}`,
      `Policy: ${finding.policyReferences.map((reference) => reference.label).join(", ") || "unsupported"}`,
      `Gaps: ${finding.gaps.join("; ") || "none"}`,
      ...(finding.dissent ? ["", `Dissent: ${finding.dissent}`] : []),
      "",
    ]),
    "## Disagreement and contradiction record",
    "",
    ...pack.contradictions.flatMap((contradiction) => [
      `### ${contradiction.title}`,
      "",
      contradiction.summary,
      "",
      `Sources: ${contradiction.sourceReferences.map((reference) => reference.evidenceId).join(", ")}`,
      `Resolution: ${contradiction.resolution ?? "unresolved"}`,
      "",
    ]),
    "## Risk register",
    "",
    "| Harm | Affected students | Inherent | Mitigation | Residual | Owner / due |",
    "|---|---|---|---|---|---|",
    ...pack.risks.map(
      (risk) =>
        `| ${escapeCell(risk.harm)} | ${escapeCell(risk.affectedSubjects)} | ${risk.inherentRating} | ${escapeCell(risk.mitigation)} | ${risk.residualRating} | ${escapeCell(`${risk.owner} / ${risk.dueDate}`)} |`,
    ),
    "",
    "## Independent verification",
    "",
    `Verdict: ${pack.verification.verdict}`,
    "",
    pack.verification.citationCoverage,
    "",
    ...pack.verification.notes.map((note) => `- ${note}`),
    "",
    "## Audit trail",
    "",
    ...pack.audit.map(
      (event) =>
        `- ${event.timestamp} — ${event.actor} (${event.role}): ${event.action} — ${event.object}${event.newValue ? ` → ${event.newValue}` : ""}`,
    ),
    "",
  ];
  return lines.join("\n");
}

function escapeCell(value: string): string {
  return value.replaceAll("|", "\\|").replaceAll("\n", " ");
}

function titleCase(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}
