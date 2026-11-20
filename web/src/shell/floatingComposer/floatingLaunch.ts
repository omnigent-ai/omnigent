// How the floating New Agent panel hands off a session it created: open it in a main window,
// then send the first message unless the create already carried it.
import { floatingComposerBridge } from "@/lib/nativeBridge";
import { randomUUID } from "@/lib/randomUUID";
import { ApiError, fetchSessionItemsPage, postEvent } from "@/lib/sessionsApi";
import { sendVerdictFromServer } from "@/lib/userInputDelivery";
import type { FloatingLaunchRequest } from "@/shell/composerPresentation";
import { uploadFileBlocks } from "@/store/chatStore";

/** The panel's next state: an error to show, and whether to start a fresh composer. */
export interface LaunchOutcome {
  error: string | null;
  freshComposer: boolean;
}

type Delivery = "delivered" | "failed" | "unknown";

export const SEND_FAILED_ERROR =
  "The new agent didn't get your message. Your prompt is still here; its session is in the sidebar.";
export const SEND_UNKNOWN_ERROR =
  "Couldn't confirm the new agent got your message. Check its session before sending it again.";
export const OPEN_FAILED_ERROR =
  "Your agent started, but Omnigent couldn't show it. Find it in the sidebar.";

/**
 * Open the launched session in a main window, then deliver its first message. Never rejects:
 * the session already exists, so every failure becomes the panel's error.
 */
export async function launchFromPanel(request: FloatingLaunchRequest): Promise<LaunchOutcome> {
  const opened = await Promise.resolve()
    .then(() => floatingComposerBridge()?.openInMainWindow(`/c/${request.sessionId}`))
    .catch(() => null);
  const delivery: Delivery = request.inputAccepted
    ? "delivered"
    : await deliverFirstMessage(request).catch(() => "unknown" as const);
  if (delivery === "failed") return { error: SEND_FAILED_ERROR, freshComposer: false };
  if (delivery === "unknown") return { error: SEND_UNKNOWN_ERROR, freshComposer: false };
  return { error: opened?.ok ? null : OPEN_FAILED_ERROR, freshComposer: true };
}

async function deliverFirstMessage({
  sessionId,
  text,
  skill,
  files,
}: FloatingLaunchRequest): Promise<Delivery> {
  const stableId = randomUUID().replace(/-/g, "");
  let fileBlocks;
  try {
    fileBlocks = skill ? [] : await uploadFileBlocks(sessionId, files ?? []);
  } catch {
    return "failed"; // Nothing was posted.
  }
  try {
    // No `holdWhenHostOffline`: the panel can't resend later, so an offline host's failure
    // is recorded in the session, where its window shows it.
    const response = await postEvent(
      sessionId,
      skill
        ? {
            type: "slash_command",
            data: { kind: "skill", name: skill.name, arguments: skill.args, stable_id: stableId },
          }
        : {
            type: "message",
            data: {
              role: "user",
              content: [
                ...fileBlocks,
                ...(text.trim() ? [{ type: "input_text" as const, text }] : []),
              ],
              stable_id: stableId,
            },
          },
    );
    return response.denied ? "failed" : "delivered";
  } catch (error) {
    // As the chat composer judges a send: a coded answer refused it; anything else may have run.
    if (error instanceof ApiError && error.code !== null) return "failed";
    return deliveryFromServer(sessionId, stableId);
  }
}

/**
 * What became of a message whose post got no clear answer. Only a recorded refusal proves it
 * failed, and only a stored message the runner didn't refuse proves it arrived; a native
 * runner takes a message before its transcript stores it, so a missing one proves nothing.
 */
async function deliveryFromServer(sessionId: string, stableId: string): Promise<Delivery> {
  const verdict = await sendVerdictFromServer(sessionId, stableId);
  if (verdict === "refused") return "failed";
  if (verdict === null) return "unknown";
  const page = await fetchSessionItemsPage(sessionId).catch(() => null);
  return page?.items.some((item) => item.id === stableId) ? "delivered" : "unknown";
}
