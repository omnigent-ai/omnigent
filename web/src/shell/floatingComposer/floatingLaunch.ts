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
  "Couldn't send your message to the new agent. Your prompt is still here, so you can try again.";
export const SEND_UNKNOWN_ERROR =
  "Couldn't confirm your message reached the new agent. Open the session before sending it again.";
export const OPEN_FAILED_ERROR =
  "Your agent started, but Omnigent couldn't show it. Find it in the sidebar.";

/** Open the launched session in a main window, then deliver its first message. */
export async function launchFromPanel(request: FloatingLaunchRequest): Promise<LaunchOutcome> {
  const opened = await floatingComposerBridge()
    ?.openInMainWindow(`/c/${request.sessionId}`)
    .catch(() => null);
  const delivery = request.inputAccepted ? "delivered" : await deliverFirstMessage(request);
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
  try {
    // A bundled skill goes as a slash command, as the chat composer sends one.
    const response = skill
      ? await postEvent(sessionId, {
          type: "slash_command",
          data: { kind: "skill", name: skill.name, arguments: skill.args, stable_id: stableId },
        })
      : await postEvent(
          sessionId,
          {
            type: "message",
            data: {
              role: "user",
              content: [
                ...(await uploadFileBlocks(sessionId, files ?? [])),
                ...(text.trim() ? [{ type: "input_text" as const, text }] : []),
              ],
              stable_id: stableId,
            },
          },
          { holdWhenHostOffline: true },
        );
    return response.denied ? "failed" : "delivered";
  } catch (error) {
    // A coded answer refused it; only a lost or uncoded answer leaves delivery unknown.
    if (error instanceof ApiError && error.code !== null) return "failed";
    if (skill) return "unknown";
    return deliveryFromServer(sessionId, stableId);
  }
}

/** What became of a message whose post got no clear answer, from the session itself. */
async function deliveryFromServer(sessionId: string, stableId: string): Promise<Delivery> {
  const page = await fetchSessionItemsPage(sessionId).catch(() => null);
  if (page === null) return "unknown";
  // The server stores a message before it answers, so a missing one was never sent.
  if (!page.items.some((item) => item.id === stableId)) return "failed";
  const verdict = await sendVerdictFromServer(sessionId, stableId);
  if (verdict === null) return "unknown";
  return verdict === "refused" ? "failed" : "delivered";
}
