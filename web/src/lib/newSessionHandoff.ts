// Copies a message into the new-session composer. Files can't ride in a URL, so
// the message waits here under a one-time token the link names.
import { randomUUID } from "@/lib/randomUUID";

/** Search param naming a handed-off message, e.g. `?handoff=<token>`. */
export const NEW_SESSION_HANDOFF_PARAM = "handoff";

/** A message copied into the new-session composer, replacing its draft. */
export interface NewSessionHandoff {
  text: string;
  files: File[];
}

const handoffs = new Map<string, NewSessionHandoff>();

/** The new-session composer's location, holding `text` and `files` as its whole draft. */
export function newSessionLocation(text: string, files: File[]): string {
  const token = randomUUID();
  handoffs.set(token, { text, files });
  return `/?${new URLSearchParams({ [NEW_SESSION_HANDOFF_PARAM]: token })}`;
}

/** The message a link's token names, or `null` for no or an unknown token. */
export function peekNewSessionHandoff(token: string | null): NewSessionHandoff | null {
  return token === null ? null : (handoffs.get(token) ?? null);
}

/** Release a token's message once the composer has taken it. */
export function forgetNewSessionHandoff(token: string | null): void {
  if (token !== null) handoffs.delete(token);
}
