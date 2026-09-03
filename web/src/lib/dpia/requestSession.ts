import { authenticatedFetch } from "@/lib/identity";
import {
  bindOnlyOnlineRunner,
  createSession,
  launchRunner,
  updateSession,
} from "@/lib/sessionsApi";
import { DPIA_PRODUCT_LABEL } from "./caseSession";

export const DPIA_ROLE_LABEL = "omnigent.dpia.role";
export const DPIA_REQUEST_ID_LABEL = "omnigent.dpia.request_id";
export const DPIA_REQUEST_STATUS_LABEL = "omnigent.dpia.request_status";
export const DPIA_CONTRIBUTOR_LABEL = "omnigent.dpia.contributor";
export const DPIA_RESPONSE_STATUS_LABEL = "omnigent.dpia.response_status";
export const DPIA_ACK_LABEL = "omnigent.dpia.ack";
export const DPIA_CASE_ID_LABEL = "omnigent.case_id";

export type DpiaSessionRole = "requester" | "contributor";
export type DpiaRequestStatus = "draft" | "submitted" | "accepted" | "declined" | "completed";
export type DpiaResponseStatus = "draft" | "submitted" | "accepted" | "rejected";

export interface DpiaLabelledSession {
  sessionId: string;
  labels: Record<string, string>;
}

interface SessionListRow {
  id: string;
  labels?: Record<string, string>;
  workspace?: string | null;
  host_id?: string | null;
  agent_name?: string | null;
}

export async function listDpiaSessionsByRole(
  role: DpiaSessionRole,
  signal?: AbortSignal,
): Promise<DpiaLabelledSession[]> {
  const response = await authenticatedFetch("/v1/sessions?limit=100", { signal });
  if (!response.ok) {
    throw new Error(`Could not inspect sessions (${response.status}).`);
  }
  const list = (await response.json()) as { data: SessionListRow[] };
  return list.data
    .filter(
      (row) =>
        row.labels?.["omnigent.product"] === DPIA_PRODUCT_LABEL &&
        row.labels?.[DPIA_ROLE_LABEL] === role,
    )
    .map((row) => ({ sessionId: row.id, labels: row.labels ?? {} }));
}

async function ensureSessionRunner(sessionId: string): Promise<void> {
  try {
    const bound = await bindOnlyOnlineRunner(sessionId);
    if (bound) return;
  } catch {
    // More than one pool runner online — fall through and launch a dedicated one.
  }
  const response = await authenticatedFetch("/v1/sessions?limit=100");
  if (!response.ok) return;
  const list = (await response.json()) as { data: SessionListRow[] };
  const template =
    list.data.find(
      (row) =>
        row.agent_name === "dpia-investigation" &&
        typeof row.workspace === "string" &&
        typeof row.host_id === "string",
    ) ??
    list.data.find((row) => typeof row.workspace === "string" && typeof row.host_id === "string");
  if (!template?.workspace || !template.host_id) return;
  await launchRunner(template.host_id, sessionId, template.workspace);
}

async function createLabelledDpiaSession(
  agentId: string,
  title: string,
  labels: Record<string, string>,
): Promise<DpiaLabelledSession> {
  const created = await createSession(agentId, [], { title });
  const labelled = await updateSession(created.id, {
    labels: { "omnigent.product": DPIA_PRODUCT_LABEL, ...labels },
  });
  if (labelled.runnerId == null) await ensureSessionRunner(labelled.id);
  return { sessionId: labelled.id, labels: { "omnigent.product": DPIA_PRODUCT_LABEL, ...labels } };
}

export function createDpiaRequestSession(agentId: string): Promise<DpiaLabelledSession> {
  return createLabelledDpiaSession(agentId, "DPIA request (draft)", {
    [DPIA_ROLE_LABEL]: "requester",
    [DPIA_REQUEST_STATUS_LABEL]: "draft",
  });
}

export interface ContributorSessionInput {
  caseId: string;
  contributor: string;
  requestId?: string;
}

export function createDpiaContributorSession(
  agentId: string,
  input: ContributorSessionInput,
): Promise<DpiaLabelledSession> {
  return createLabelledDpiaSession(
    agentId,
    `DPIA outreach: ${input.contributor} (${input.caseId})`,
    {
      [DPIA_ROLE_LABEL]: "contributor",
      [DPIA_CASE_ID_LABEL]: input.caseId,
      [DPIA_CONTRIBUTOR_LABEL]: input.contributor,
      [DPIA_RESPONSE_STATUS_LABEL]: "draft",
      ...(input.requestId === undefined ? {} : { [DPIA_REQUEST_ID_LABEL]: input.requestId }),
    },
  );
}

export async function setDpiaSessionLabels(
  sessionId: string,
  labels: Record<string, string>,
): Promise<void> {
  await updateSession(sessionId, { labels });
}

export async function renameDpiaSession(sessionId: string, title: string): Promise<void> {
  const response = await authenticatedFetch(`/v1/sessions/${sessionId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!response.ok) {
    throw new Error(`Could not rename the request session (${response.status}).`);
  }
}

const REQUESTER_PREFIX = "DPIA requester message follows:\n";
const CONTRIBUTOR_PREFIX = "DPIA contributor message follows:\n";
const OFFICER_PREFIX = "DPIA officer message follows:\n";

export function buildRequesterAgentMessage(text: string): string {
  return `${REQUESTER_PREFIX}${text}`;
}

export function buildContributorAgentMessage(text: string): string {
  return `${CONTRIBUTOR_PREFIX}${text}`;
}

export function buildOfficerRelayMessage(text: string): string {
  return `${OFFICER_PREFIX}${text}`;
}

export interface DisplayMessage {
  text: string;
  sender: "participant" | "officer";
}

export function displayMessageFromWireText(message: string): DisplayMessage {
  for (const prefix of [REQUESTER_PREFIX, CONTRIBUTOR_PREFIX]) {
    if (message.startsWith(prefix)) {
      return { text: message.slice(prefix.length).trim(), sender: "participant" };
    }
  }
  if (message.startsWith(OFFICER_PREFIX)) {
    return { text: message.slice(OFFICER_PREFIX.length).trim(), sender: "officer" };
  }
  return { text: message, sender: "participant" };
}

export interface ContributorIntroInput {
  caseId: string;
  caseTitle: string;
  contributor: string;
  requestId?: string;
  questions: { id: string; text: string }[];
}

export function buildContributorIntroMessage(input: ContributorIntroInput): string {
  return [
    OFFICER_PREFIX.trimEnd(),
    `The Privacy Officer is sharing scoped questions for DPIA case "${input.caseTitle}" (${input.caseId}) with the ${input.contributor} stakeholder.`,
    "Scoped questions JSON:",
    JSON.stringify(
      input.questions.map((question) => ({ question_id: question.id, text: question.text })),
    ),
    "Greet the stakeholder, walk through each scoped question conversationally, and only discuss this scoped context.",
    `When the stakeholder confirms their answers, emit exactly one raw JSON object matching schemas/stakeholder-response.schema.json with artifact "stakeholder-response", case_id "${input.caseId}"${
      input.requestId === undefined ? "" : `, request_id "${input.requestId}"`
    }, the stakeholder as respondent, and one answer per scoped question_id. No prose around the JSON.`,
  ].join("\n");
}

export function scopedQuestionsFromIntro(text: string): { id: string; text: string }[] | null {
  const marker = "Scoped questions JSON:\n";
  const start = text.indexOf(marker);
  if (start === -1) return null;
  const jsonLine = text.slice(start + marker.length).split("\n")[0];
  try {
    const parsed: unknown = JSON.parse(jsonLine);
    if (!Array.isArray(parsed)) return null;
    const questions = parsed.flatMap((entry) =>
      typeof entry === "object" &&
      entry !== null &&
      typeof (entry as { question_id?: unknown }).question_id === "string" &&
      typeof (entry as { text?: unknown }).text === "string"
        ? [
            {
              id: (entry as { question_id: string }).question_id,
              text: (entry as { text: string }).text,
            },
          ]
        : [],
    );
    return questions.length > 0 ? questions : null;
  } catch {
    return null;
  }
}
