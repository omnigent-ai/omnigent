// How a user message's delivery is judged: whether the runner refused a send,
// read from a session snapshot when its POST got no answer.
import { getSessionSlim } from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";

/**
 * Whether a session snapshot records the runner refusing the send `stableId`.
 *
 * An attributed rejection names its item and holds whatever the session is
 * doing now; the unattributed record of a server predating the item id stands
 * for any send, but only while the session is still `failed` by it.
 *
 * @param session - A session snapshot.
 * @param stableId - The send's stable id (its persisted item id).
 * @returns `true` when the snapshot's `runner_rejected_event` applies to it.
 */
export function snapshotRefusesSend(session: Session, stableId: string): boolean {
  const rejection = session.lastTaskError;
  if (rejection?.code !== "runner_rejected_event") return false;
  if (rejection.item_id !== undefined) return rejection.item_id === stableId;
  return session.status === "failed";
}

/**
 * Ask the server what became of a send whose POST got no answer.
 *
 * The server records a runner rejection before it answers the POST, so a
 * snapshot fetched after the failure is conclusive where one merged during the
 * send may not be.
 *
 * @param sessionId - The session the send was posted to.
 * @param stableId - The send's stable id.
 * @returns `"refused"` or `"delivered"`, or `null` when the fetch itself fails.
 */
export async function sendVerdictFromServer(
  sessionId: string,
  stableId: string,
): Promise<"refused" | "delivered" | null> {
  try {
    const session = await getSessionSlim(sessionId);
    return snapshotRefusesSend(session, stableId) ? "refused" : "delivered";
  } catch {
    return null;
  }
}
