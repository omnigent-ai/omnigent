import type { DpiaCaseSnapshot } from "./types";

export type DpiaAttentionKind =
  | "pending-proposal"
  | "stale-finding"
  | "failed-live-run"
  | "awaited-decision"
  | "incoming-request"
  | "pending-response";

export interface DpiaAttentionItem {
  id: string;
  kind: DpiaAttentionKind;
  caseId: string;
  title: string;
  detail: string;
  severity: "high" | "medium" | "low";
  timestamp: string;
  href: string;
}

export function deriveDpiaAttentionItems(caseData: DpiaCaseSnapshot): DpiaAttentionItem[] {
  const casePath = `/dpia/cases/${caseData.id}`;
  const proposals: DpiaAttentionItem[] = (caseData.correctionProposals ?? [])
    .filter(({ status }) => status === "pending")
    .map((record) => ({
      id: `proposal-${record.id}`,
      kind: "pending-proposal",
      caseId: caseData.id,
      title: "Correction proposal needs officer review",
      detail: record.proposal.instruction,
      severity: "medium",
      timestamp: record.createdAt,
      href: `${casePath}?tab=overview&attention=${encodeURIComponent(record.id)}`,
    }));
  const staleFindings: DpiaAttentionItem[] = caseData.determinations
    .filter(({ status }) => status === "stale-after-change")
    .map((finding) => ({
      id: `stale-${finding.id}`,
      kind: "stale-finding",
      caseId: caseData.id,
      title: `${finding.dimensionId.replaceAll("-", " ")} finding is stale`,
      detail: finding.staleReason ?? "Reassess this finding against the current processing model.",
      severity: "high",
      timestamp: caseData.updatedAt,
      href: `${casePath}?tab=screening&finding=${encodeURIComponent(finding.id)}`,
    }));
  const failedRun: DpiaAttentionItem[] =
    caseData.liveRun?.status === "failed"
      ? [
          {
            id: `live-run-${caseData.id}`,
            kind: "failed-live-run",
            caseId: caseData.id,
            title: "Live DPIA investigation failed",
            detail: caseData.liveRun.message,
            severity: "high",
            timestamp: caseData.liveRun.updatedAt,
            href: `${casePath}?tab=overview&agentActivity=1`,
          },
        ]
      : [];
  const awaitedDecision: DpiaAttentionItem[] = caseData.officerDecision
    ? []
    : [
        {
          id: `decision-${caseData.id}`,
          kind: "awaited-decision",
          caseId: caseData.id,
          title: "Screening recommendation awaits officer decision",
          detail: `Recommendation: ${caseData.recommendation.replaceAll("-", " ")}.`,
          severity: "medium",
          timestamp: caseData.updatedAt,
          href: `${casePath}?tab=screening`,
        },
      ];
  return [...proposals, ...staleFindings, ...failedRun, ...awaitedDecision].sort((left, right) =>
    right.timestamp.localeCompare(left.timestamp),
  );
}

export function countDpiaAttentionItems(cases: DpiaCaseSnapshot[]): number {
  return cases.reduce((total, caseData) => total + deriveDpiaAttentionItems(caseData).length, 0);
}
