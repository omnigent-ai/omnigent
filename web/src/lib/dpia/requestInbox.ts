import { itemsToBlocks } from "@/lib/itemsToBlocks";
import { buildBubbles, type Bubble } from "@/lib/renderItems";
import { fetchSessionItemsPage } from "@/lib/sessionsApi";
import type { MessageContentBlock } from "@/lib/blocks";
import {
  latestArtifact,
  parseDpiaOutcomeText,
  parseDpiaRequestText,
  parseStakeholderResponseText,
  type DpiaOutcome,
  type DpiaRequest,
  type StakeholderResponse,
} from "./requestArtifacts";
import {
  DPIA_ACK_LABEL,
  DPIA_CASE_ID_LABEL,
  DPIA_CONTRIBUTOR_LABEL,
  DPIA_REQUEST_ID_LABEL,
  DPIA_REQUEST_STATUS_LABEL,
  DPIA_RESPONSE_STATUS_LABEL,
  listDpiaSessionsByRole,
  type DpiaRequestStatus,
  type DpiaResponseStatus,
} from "./requestSession";

export function bubbleText(bubble: Bubble): string {
  if (bubble.kind === "user") {
    return bubble.content
      .filter(
        (block): block is Extract<MessageContentBlock, { type: "input_text" | "output_text" }> =>
          block.type === "input_text" || block.type === "output_text",
      )
      .map((block) => block.text)
      .join("\n");
  }
  if (bubble.kind !== "assistant") return "";
  return bubble.items
    .filter((item) => item.kind === "text")
    .map((item) => item.text)
    .join("");
}

export async function fetchSessionTexts(sessionId: string): Promise<string[]> {
  const page = await fetchSessionItemsPage(sessionId, { limit: 60 });
  return buildBubbles(itemsToBlocks(page.items), null)
    .map(bubbleText)
    .filter((text) => text.length > 0);
}

export interface DpiaRequestSummary {
  sessionId: string;
  requestId: string | null;
  status: DpiaRequestStatus;
  acknowledged: boolean;
  caseId: string | null;
  request: DpiaRequest | null;
  outcome: DpiaOutcome | null;
}

export async function fetchDpiaRequestSummaries(
  signal?: AbortSignal,
): Promise<DpiaRequestSummary[]> {
  const sessions = await listDpiaSessionsByRole("requester", signal);
  const submitted = sessions.filter(
    (session) => (session.labels[DPIA_REQUEST_STATUS_LABEL] ?? "draft") !== "draft",
  );
  const summaries = await Promise.all(
    submitted.map(async (session): Promise<DpiaRequestSummary> => {
      const texts = await fetchSessionTexts(session.sessionId);
      return {
        sessionId: session.sessionId,
        requestId: session.labels[DPIA_REQUEST_ID_LABEL] ?? null,
        status: (session.labels[DPIA_REQUEST_STATUS_LABEL] ?? "submitted") as DpiaRequestStatus,
        acknowledged: session.labels[DPIA_ACK_LABEL] === "true",
        caseId: session.labels[DPIA_CASE_ID_LABEL] ?? null,
        request: latestArtifact(texts, parseDpiaRequestText),
        outcome: latestArtifact(texts, parseDpiaOutcomeText),
      };
    }),
  );
  return summaries.sort((left, right) =>
    (right.request?.submitted_at ?? "").localeCompare(left.request?.submitted_at ?? ""),
  );
}

export interface DpiaContributorSummary {
  sessionId: string;
  contributor: string;
  caseId: string;
  requestId: string | null;
  status: DpiaResponseStatus;
  response: StakeholderResponse | null;
}

export async function fetchDpiaContributorSummaries(
  caseId: string,
  signal?: AbortSignal,
): Promise<DpiaContributorSummary[]> {
  const sessions = await listDpiaSessionsByRole("contributor", signal);
  const scoped = sessions.filter((session) => session.labels[DPIA_CASE_ID_LABEL] === caseId);
  return Promise.all(
    scoped.map(async (session): Promise<DpiaContributorSummary> => {
      const texts = await fetchSessionTexts(session.sessionId);
      return {
        sessionId: session.sessionId,
        contributor: session.labels[DPIA_CONTRIBUTOR_LABEL] ?? "Stakeholder",
        caseId,
        requestId: session.labels[DPIA_REQUEST_ID_LABEL] ?? null,
        status: (session.labels[DPIA_RESPONSE_STATUS_LABEL] ?? "draft") as DpiaResponseStatus,
        response: latestArtifact(texts, parseStakeholderResponseText),
      };
    }),
  );
}
