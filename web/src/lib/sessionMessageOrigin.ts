// ``sys_session_message`` prefixes each delivered message with its sender's id
// (``orchestration_message`` in omnigent/server/session_orchestration.py).
const SESSION_MESSAGE_PREFIX_RE = /^\[Message from session ([A-Za-z0-9_-]+)\]\r?\n/;

/** A message another session sent, split into its sender and its text. */
export interface SessionMessageOrigin {
  sourceId: string;
  body: string;
}

/**
 * Split the sender prefix off a message delivered by ``sys_session_message``.
 *
 * The prefix is a display label written by the server, not proof of origin.
 *
 * :param text: Message text, e.g. ``"[Message from session 9b8b…]\nhi"``.
 * :returns: ``{sourceId: "9b8b…", body: "hi"}``, or ``null`` without the prefix.
 */
export function splitSessionMessageOrigin(text: string): SessionMessageOrigin | null {
  const match = SESSION_MESSAGE_PREFIX_RE.exec(text);
  if (!match) return null;
  return { sourceId: match[1], body: text.slice(match[0].length) };
}
