import { readAlwaysSteer } from "@/lib/alwaysSteerPreferences";
import { shouldQueueSend } from "@/lib/messageQueue";
import { holdsInitialDraft, useChatStore } from "@/store/chatStore";

const requestsInFlight = new Set<string>();

async function sendDocumentRequest(sessionId: string, text: string) {
  const chat = useChatStore.getState();
  if (chat.conversationId !== sessionId || !chat.boundAgentId) {
    throw new Error("Wait for this chat to connect before sending a document request.");
  }
  const key = JSON.stringify([sessionId, text]);
  if (
    requestsInFlight.has(key) ||
    chat.queuedMessages.some(
      (message) => message.conversationId === sessionId && message.text === text,
    ) ||
    chat.pendingUserMessages.some((message) =>
      message.content.some((part) => part.type === "input_text" && part.text === text),
    )
  )
    return;

  if (
    shouldQueueSend(
      sessionId,
      chat.status,
      chat.sessionStatus,
      chat.queuedMessages,
      readAlwaysSteer(),
      false,
      holdsInitialDraft(chat),
    )
  ) {
    chat.enqueueMessage(text, undefined, undefined, sessionId);
    return;
  }

  requestsInFlight.add(key);
  try {
    let failure: string | undefined;
    await chat.send(text, chat.boundAgentId, undefined, {
      pinnedConversationId: sessionId,
      external: true,
      onDenied: () => {
        failure = "The session’s policy blocked the document request.";
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

export function requestGoogleDocRevision(sessionId: string, url: string): Promise<void> {
  return sendDocumentRequest(
    sessionId,
    `Review the latest changes and comments in this Google Doc: ${url}\nUse the session’s authorized Google tools to address feedback through appropriate edits and native comment-thread replies where permitted. If Google access is read-only or does not permit an edit or reply, provide findings and proposed changes in chat instead. Report the outcome in chat, including if you lack access.`,
  );
}
