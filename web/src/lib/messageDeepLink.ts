/** Query param for deep-linking to a transcript message. */
export const MESSAGE_QUERY_PARAM = "message";
/** Changes with each in-app request, so asking for the same message again scrolls again. */
export const MESSAGE_REQUEST_PARAM = "message_request";

/**
 * Build a shareable URL for a message in the current session.
 *
 * Preserves the current path and other search params; sets/replaces
 * ``?message=<id>``. Auth is unchanged - the link only encodes location.
 *
 * @param messageId - Stable message id (user ``itemId`` or assistant ``responseId``).
 * @param href - Current location; defaults to ``window.location.href``.
 */
export function buildMessageDeepLink(messageId: string, href?: string): string {
  const url = new URL(href ?? window.location.href);
  url.searchParams.set(MESSAGE_QUERY_PARAM, messageId);
  return url.toString();
}

/**
 * Find the transcript DOM node stamped with ``data-message-id``: the newest
 * one, since one of the agent's own tasks can mark the same start twice.
 *
 * @param messageId - The id encoded in ``?message=``.
 */
export function findMessageElement(messageId: string): Element | null {
  const matches = document.querySelectorAll(`[data-message-id="${CSS.escape(messageId)}"]`);
  return matches[matches.length - 1] ?? null;
}

/**
 * Search params that ask the transcript to scroll to and flash *messageId*,
 * from another surface such as the Tasks tab, even if the URL already names it.
 */
export function messageDeepLinkParams(
  current: URLSearchParams,
  messageId: string,
): URLSearchParams {
  const next = new URLSearchParams(current);
  next.set(MESSAGE_QUERY_PARAM, messageId);
  next.set(MESSAGE_REQUEST_PARAM, String(Date.now()));
  return next;
}
