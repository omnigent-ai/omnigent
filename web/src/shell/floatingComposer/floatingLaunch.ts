// How the floating New Agent panel hands off a session it created: open it in a main window,
// then send the first message unless the create already carried it or that window sends it.
import {
  floatingComposerBridge,
  type FloatingComposerBridge,
  type FloatingLaunchMessage,
  type FloatingLaunchOffer,
} from "@/lib/nativeBridge";
import { randomUUID } from "@/lib/randomUUID";
import { ApiError, fetchSessionItemsPage, postEvent } from "@/lib/sessionsApi";
import { sendVerdictFromServer } from "@/lib/userInputDelivery";
import type { FloatingLaunchRequest } from "@/shell/composerPresentation";
import {
  isListeningForInitialPrompt,
  setPendingInitialPrompt,
  type PendingInitialPrompt,
  uploadFileBlocks,
} from "@/store/chatStore";

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

/** The most a launch's attachments may total; the shell checks the same cap. */
export const LAUNCH_MESSAGE_MAX_BYTES = 25 * 1024 * 1024;

/**
 * Open the launched session in a main window, then deliver its first message unless that window
 * sends it. Never rejects: the session already exists, so every failure becomes the panel's error.
 */
export async function launchFromPanel(request: FloatingLaunchRequest): Promise<LaunchOutcome> {
  const bridge = floatingComposerBridge();
  const message = await launchMessageForShell(bridge, request);
  const opening = await Promise.resolve()
    .then(() => bridge?.openInMainWindow(`/c/${request.sessionId}`, message))
    .then(
      (result) => ({ result: result ?? null, rejected: false }),
      () => ({ result: null, rejected: true }),
    );
  // A rejected open with a message in hand may already have been confirmed, so it isn't resent.
  if (opening.rejected && message !== null) {
    return { error: SEND_UNKNOWN_ERROR, freshComposer: false };
  }
  const opened = opening.result;
  const handedOff = opened?.ok === true && opened.delivery === "page";
  const delivery: Delivery =
    request.inputAccepted || handedOff
      ? "delivered"
      : await deliverFirstMessage(request).catch(() => "unknown" as const);
  if (delivery === "failed") return { error: SEND_FAILED_ERROR, freshComposer: false };
  if (delivery === "unknown") return { error: SEND_UNKNOWN_ERROR, freshComposer: false };
  return { error: opened?.ok ? null : OPEN_FAILED_ERROR, freshComposer: true };
}

/**
 * The launch's first message for the shell to send, or null when the panel delivers it: the
 * create carried it, the shell can't take it, or its attachments are too large or unreadable.
 * Asks the shell before reading any attachment bytes.
 */
async function launchMessageForShell(
  bridge: FloatingComposerBridge | undefined,
  request: FloatingLaunchRequest,
): Promise<FloatingLaunchMessage | null> {
  if (request.inputAccepted || !bridge?.canHandOffLaunch) return null;
  try {
    if (!(await bridge.canHandOffLaunch())) return null;
    const files = request.skill ? [] : (request.files ?? []);
    if (files.reduce((total, file) => total + file.size, 0) > LAUNCH_MESSAGE_MAX_BYTES) return null;
    return {
      text: request.text,
      skill: request.skill ? { name: request.skill.name, args: request.skill.args } : null,
      files: await Promise.all(
        files.map(async (file) => ({
          name: file.name,
          type: file.type,
          bytes: new Uint8Array(await file.arrayBuffer()),
        })),
      ),
    };
  } catch (err) {
    console.warn("[floatingComposer] couldn't prepare a launch's message:", err);
    return null;
  }
}

/**
 * A main window takes the first message a panel launch offered it, rebuilds its attachments, and
 * confirms. Its send starts only after a confirm the shell accepts; any other outcome leaves the
 * message to the panel. Never rejects.
 */
export async function acceptLaunchOffer({
  offerId,
  sessionId,
}: FloatingLaunchOffer): Promise<void> {
  const bridge = floatingComposerBridge();
  if (!bridge?.takeLaunchOffer || !bridge.confirmLaunchOffer) return;
  // Ends the offer now, so the panel delivers without waiting out the timeout.
  const decline = () => {
    Promise.resolve()
      .then(() => bridge.declineLaunchOffer?.(offerId))
      .catch((err) => console.warn("[floatingComposer] couldn't decline a launch:", err));
  };
  try {
    const message = await bridge.takeLaunchOffer(offerId);
    if (message === null) return;
    // No page shows the session to send it, so the panel delivers instead.
    if (!isListeningForInitialPrompt(sessionId)) {
      decline();
      return;
    }
    const prompt: PendingInitialPrompt = {
      text: message.text,
      skill: message.skill,
      files: message.files.map(({ name, type, bytes }) => new File([bytes], name, { type })),
    };
    if (!(await bridge.confirmLaunchOffer(offerId))) return;
    setPendingInitialPrompt(sessionId, prompt);
  } catch (err) {
    console.warn("[floatingComposer] couldn't take a launch's message:", err);
    decline();
  }
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
