import {
  buildGoogleDocsLabelPatch,
  readGoogleDocsLabels,
  type GoogleDocsSessionState,
} from "./googleDocs";
import { getSessionLabels, updateSession } from "./sessionsApi";

export interface GoogleDocsSessionRequest {
  /** The canonical ID from the session snapshot. */
  readonly sessionId: string;
  /** Compare with the latest request for the active view before applying the result. */
  readonly generation: number;
  readonly result: Promise<GoogleDocsSessionState>;
}

// A pane remount must queue behind writes from its previous instance.
const pendingBySession = new Map<string, Promise<void>>();
let generation = 0;

function enqueue(
  sessionId: string,
  operation: () => Promise<GoogleDocsSessionState>,
): GoogleDocsSessionRequest {
  const result = (pendingBySession.get(sessionId) ?? Promise.resolve()).then(operation);
  const clear = () => {
    if (pendingBySession.get(sessionId) === tail) pendingBySession.delete(sessionId);
  };
  const tail = result.then(clear, clear);
  pendingBySession.set(sessionId, tail);
  return { sessionId, generation: ++generation, result };
}

async function readLabels(sessionId: string): Promise<Record<string, string>> {
  const response = await getSessionLabels(sessionId);
  if (response.id !== sessionId || !response.labels) {
    throw new Error("Could not read labels for this session");
  }
  return response.labels;
}

/** Read the persisted state, including after a remount or a shared-session update. */
export function loadGoogleDocsSession(sessionId: string): GoogleDocsSessionRequest {
  return enqueue(sessionId, async () => readGoogleDocsLabels(await readLabels(sessionId)));
}

/** Save a choice and resolve with the state confirmed by PATCH. Null unlinks. */
export function saveGoogleDocsSession(
  sessionId: string,
  url: string | null,
): GoogleDocsSessionRequest {
  return enqueue(sessionId, async () => {
    // A failed PATCH may have committed, and other collaborators can also edit the labels.
    const labels = buildGoogleDocsLabelPatch(await readLabels(sessionId), url);
    const updated = await updateSession(sessionId, { labels });
    const confirmed = updated.labels;
    if (
      updated.id !== sessionId ||
      !confirmed ||
      Object.entries(labels).some(([key, value]) => (confirmed[key] ?? "") !== value)
    ) {
      throw new Error("Could not confirm the Google Docs selection was saved");
    }
    return readGoogleDocsLabels(confirmed);
  });
}
