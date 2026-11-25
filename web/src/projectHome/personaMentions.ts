import { personMentionMarkdown } from "@/lib/mentionPills";

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

/** A way to write an @mention (a name, sign-in name or ID) and the name its pill shows. */
export interface MentionForm {
  form: string;
  label: string;
}

/**
 * Turn each `@form` that names someone in *forms* into a mention pill, outside code.
 * Forms match case-insensitively and whole-word, longest first; other `@text` stays.
 */
export function chipMentions(text: string, forms: readonly MentionForm[]): string {
  if (!forms.length || !text.includes("@")) return text;
  const labels = new Map(forms.map(({ form, label }) => [form.toLowerCase(), label]));
  const sorted = [...forms.map(({ form }) => form)].sort((a, b) => b.length - a.length);
  const pattern = new RegExp(
    `(?<![\\w@\\[])@(${sorted.map(escapeRegExp).join("|")})(?![\\w@])`,
    "gi",
  );
  // Odd segments are fenced or inline code; leave those untouched.
  return text
    .split(/(```[\s\S]*?```|`[^`\n]*`)/)
    .map((segment, index) =>
      index % 2
        ? segment
        : segment.replace(pattern, (match, form: string) => {
            const label = labels.get(form.toLowerCase());
            return label ? personMentionMarkdown(label) : match;
          }),
    )
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

/** A person cc'd in a ticket comment, as the server records it. */
export interface PersonCc {
  user_id: string;
  name: string;
}

/** A readable name from a sign-in ID, matching the server: `ann.lee@example.com` reads `Ann Lee`. */
export function personDisplayName(userId: string): string {
  const local = userId.split("@", 1)[0] ?? userId;
  const words = local.split(/[._\-+]+/).filter(Boolean);
  return words.map((word) => word.charAt(0).toUpperCase() + word.slice(1)).join(" ") || userId;
}

/** Read the people a comment cc'd from its event payload. */
export function payloadCc(payload: Record<string, unknown>): PersonCc[] {
  const raw = payload.cc;
  if (!Array.isArray(raw)) return [];
  return raw.filter(
    (entry): entry is PersonCc =>
      typeof entry === "object" &&
      entry !== null &&
      typeof (entry as PersonCc).user_id === "string" &&
      typeof (entry as PersonCc).name === "string",
  );
}
