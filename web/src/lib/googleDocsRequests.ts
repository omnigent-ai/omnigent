import { readAlwaysSteer } from "./alwaysSteerPreferences";
import { classifyGoogleDocsUrl } from "./googleDocs";
import { sessionDocumentsScope } from "./sessionDocuments";
import { getOmnigentHostConfig } from "./host";
import { shouldQueueSend } from "./messageQueue";
import { isEditorLevel } from "./permissionsApi";
import { holdsInitialDraft, useChatStore } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";

const requestsInFlight = new Set<string>();

/** Use the normal pinned send/queue so review requests share chat policy and delivery errors. */
export async function requestGoogleDocReview(
  sessionId: string,
  url: string,
  permissionLevel: number | null,
  isCurrent: () => boolean,
): Promise<void> {
  if (!isCurrent())
    throw new Error("This document review belongs to a previous session activation.");
  if (!isEditorLevel(permissionLevel))
    throw new Error("Session edit access is required to request agent review.");
  const link = classifyGoogleDocsUrl(url);
  if (link.kind !== "embed")
    throw new Error("Open a supported Google Doc before requesting review.");
  const chat = useChatStore.getState();
  const session =
    chat.conversationId === sessionId ? chat : conversationRegistry.peek(sessionId)?.getState();
  if (!session?.boundAgentId)
    throw new Error("Wait for this chat to connect before requesting review.");
  const text = `Review this Google Doc and its latest changes and comments: ${url.trim()}\nAddress feedback with appropriate edits and replies in native comment threads where permitted. If the Doc is read-only or commenting is unavailable, provide findings and suggested changes in chat. Report missing Google access in chat.`;
  const scope = sessionDocumentsScope(sessionId);
  const fetcher = getOmnigentHostConfig().fetcher;
  const requireCurrentScope = () => {
    if (
      !isCurrent() ||
      sessionDocumentsScope(sessionId) !== scope ||
      getOmnigentHostConfig().fetcher !== fetcher
    )
      throw new Error(
        "The connected user or backend changed. Request review again from the current session.",
      );
  };
  const sendOptions = {
    external: true,
    beforePost: async () => {
      requireCurrentScope();
      return null;
    },
    beforeDispatch: requireCurrentScope,
  };
  const key = JSON.stringify([scope, text]);
  if (
    requestsInFlight.has(key) ||
    chat.queuedMessages.some(
      (message) => message.conversationId === sessionId && message.text === text,
    ) ||
    session.pendingUserMessages.some((message) =>
      message.content.some((part) => part.type === "input_text" && part.text === text),
    )
  )
    return;
  if (
    shouldQueueSend(
      sessionId,
      session.status,
      session.sessionStatus,
      chat.queuedMessages,
      readAlwaysSteer(),
      false,
      holdsInitialDraft(session),
    )
  ) {
    chat.enqueueMessage(text, undefined, undefined, sessionId, { sendOptions });
    return;
  }
  requestsInFlight.add(key);
  try {
    let failure: string | undefined;
    await chat.send(text, session.boundAgentId, undefined, {
      ...sendOptions,
      pinnedConversationId: sessionId,
      onDenied: () => {
        failure = "The session’s policy blocked the document review request.";
      },
      onError: (message, refused) => {
        failure = refused ? message : `${message} Check the chat for the request before retrying.`;
      },
    });
    if (failure !== undefined) throw new Error(failure);
  } finally {
    requestsInFlight.delete(key);
  }
}
