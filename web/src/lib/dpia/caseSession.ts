import { authenticatedFetch } from "@/lib/identity";
import { bindOnlyOnlineRunner, createSession, updateSession } from "@/lib/sessionsApi";
import type { SessionStatus } from "@/lib/types";

export const DPIA_PRODUCT_LABEL = "dpia-investigation";

interface SessionListRow {
  id: string;
  runner_id?: string | null;
  status?: SessionStatus;
  labels?: Record<string, string>;
}

interface SessionListResponse {
  data: SessionListRow[];
}

export interface DpiaCaseSessionBinding {
  sessionId: string;
  status: SessionStatus | null;
  created: boolean;
}

export async function findDpiaCaseSession(
  caseId: string,
  signal?: AbortSignal,
): Promise<DpiaCaseSessionBinding | null> {
  const response = await authenticatedFetch("/v1/sessions?limit=100", { signal });
  if (!response.ok) {
    throw new Error(`Could not inspect Omnigent sessions (${response.status}).`);
  }
  const list = (await response.json()) as SessionListResponse;
  const session = list.data.find(
    (candidate) =>
      candidate.labels?.["omnigent.product"] === DPIA_PRODUCT_LABEL &&
      candidate.labels?.["omnigent.case_id"] === caseId,
  );
  if (!session) return null;
  if (session.runner_id == null) {
    const bound = await bindOnlyOnlineRunner(session.id);
    if (bound) return { sessionId: bound.id, status: bound.status, created: false };
  }
  return { sessionId: session.id, status: session.status ?? null, created: false };
}

export async function findOrCreateDpiaCaseSession(
  caseId: string,
  agentId: string,
  signal?: AbortSignal,
): Promise<DpiaCaseSessionBinding> {
  const existing = await findDpiaCaseSession(caseId, signal);
  if (existing) return existing;

  const created = await createSession(agentId, [], { title: `DPIA: ${caseId}` });
  const labelled = await updateSession(created.id, {
    labels: {
      "omnigent.product": DPIA_PRODUCT_LABEL,
      "omnigent.case_id": caseId,
    },
  });
  const bound = labelled.runnerId == null ? await bindOnlyOnlineRunner(labelled.id) : labelled;
  return { sessionId: labelled.id, status: bound?.status ?? labelled.status, created: true };
}
