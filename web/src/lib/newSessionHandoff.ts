// Opens the new-session composer prefilled with a message from elsewhere in the
// app. Text rides in the existing `?prompt=` link; files can't go in a URL, so
// they wait here under a one-time token the link names.
import { randomUUID } from "@/lib/randomUUID";

/** Search param naming the handed-off attachments, e.g. `?attachments=<token>`. */
export const NEW_SESSION_ATTACHMENTS_PARAM = "attachments";

const handedOffAttachments = new Map<string, File[]>();

/**
 * The location of the new-session composer prefilled with `text` and `files`.
 *
 * @param text - Composer text, e.g. ``"Summarize the open PRs"``.
 * @param files - Attachments for the composer; none adds no token.
 * @returns A router location such as ``"/?prompt=Summarize&attachments=<token>"``.
 */
export function newSessionLocation(text: string, files: File[]): string {
  const search = new URLSearchParams({ prompt: text });
  if (files.length > 0) {
    const token = randomUUID();
    handedOffAttachments.set(token, files);
    search.set(NEW_SESSION_ATTACHMENTS_PARAM, token);
  }
  return `/?${search.toString()}`;
}

/** The attachments a link's token names, or `null` for no or an unknown token. */
export function peekNewSessionAttachments(token: string | null): File[] | null {
  return token === null ? null : (handedOffAttachments.get(token) ?? null);
}

/** Release a token's attachments once the composer has taken them. */
export function forgetNewSessionAttachments(token: string | null): void {
  if (token !== null) handedOffAttachments.delete(token);
}
