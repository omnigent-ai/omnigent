/** A persona cc'd in a ticket comment, as the server records it. */
export interface PersonaMention {
  id: string;
  name: string;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Read the server-recorded mentions from a comment event's payload. */
export function payloadMentions(payload: Record<string, unknown>): PersonaMention[] {
  const raw = payload.mentions;
  if (!Array.isArray(raw)) return [];
  return raw.filter(
    (entry): entry is PersonaMention =>
      typeof entry === "object" &&
      entry !== null &&
      typeof (entry as PersonaMention).id === "string" &&
      typeof (entry as PersonaMention).name === "string",
  );
}

/** Bold each recorded `@Name` outside code, so the mention stands out in rendered markdown. */
export function emphasizeMentions(text: string, mentions: PersonaMention[]): string {
  if (!mentions.length) return text;
  const names = [...mentions].sort((a, b) => b.name.length - a.name.length).map((m) => m.name);
  const pattern = new RegExp(`(?<![\\w@*])@(${names.map(escapeRegExp).join("|")})(?![\\w])`, "gi");
  // Odd segments are fenced or inline code; leave those untouched.
  return text
    .split(/(```[\s\S]*?```|`[^`\n]*`)/)
    .map((segment, index) => (index % 2 ? segment : segment.replace(pattern, "**@$1**")))
    .join("");
}

/** The `@query` being typed at the caret, if any: where it starts and what follows the `@`. */
export function mentionQueryAt(
  text: string,
  caret: number,
): { start: number; query: string } | null {
  const before = text.slice(0, caret);
  const match = /(^|[\s(])@([^\s@]{0,30})$/.exec(before);
  if (!match) return null;
  return { start: caret - match[2].length - 1, query: match[2] };
}

/** Replace the `@query` at the caret with `@Name ` and return the new text and caret. */
export function insertMention(
  text: string,
  caret: number,
  start: number,
  name: string,
): { text: string; caret: number } {
  const after = text.slice(caret).replace(/^\S*/, "");
  // Reuse a following space rather than doubling it; the caret lands after it.
  const inserted = after.startsWith(" ") ? `@${name}` : `@${name} `;
  return { text: text.slice(0, start) + inserted + after, caret: start + name.length + 2 };
}
