import type { SentMention } from "@/lib/composerMentions";

// A markdown link whose title starts with this renders as a mention pill.
const MENTION_TITLE_PREFIX = "omnigent-mention:";

const MARKDOWN_SPECIAL_RE = /[\\`*_[\]<>~|#]/g;

/** Inline markdown for a sent mention, rendered as a pill by ``MentionPill``. */
export function mentionPillMarkdown(mention: SentMention): string {
  if (mention.kind === "session") {
    const label = `@${mention.title}`.replace(MARKDOWN_SPECIAL_RE, "\\$&");
    const sub = mention.isSubagent ? "yes" : "no";
    // A fragment href survives link hardening untouched; the id rides in the title.
    return `[${label}](#mention "${MENTION_TITLE_PREFIX}session:${sub}:${mention.id}")`;
  }
  const label = `@${mention.path}`.replace(MARKDOWN_SPECIAL_RE, "\\$&");
  return `[${label}](#mention "${MENTION_TITLE_PREFIX}file:${encodeURIComponent(mention.path)}")`;
}

/** What a mention link's title encodes, or ``null`` for an ordinary link. */
export type MentionPillTarget =
  | { kind: "session"; id: string; isSubagent: boolean }
  | { kind: "file"; path: string; isDir: boolean; lineSpan?: string };

export function parseMentionPillTitle(title: unknown): MentionPillTarget | null {
  if (typeof title !== "string" || !title.startsWith(MENTION_TITLE_PREFIX)) return null;
  const rest = title.slice(MENTION_TITLE_PREFIX.length);
  const session = /^session:(yes|no):([A-Za-z0-9_:-]+)$/.exec(rest);
  if (session) return { kind: "session", id: session[2], isSubagent: session[1] === "yes" };
  if (!rest.startsWith("file:")) return null;
  let raw: string;
  try {
    raw = decodeURIComponent(rest.slice("file:".length));
  } catch {
    return null;
  }
  // "path:3-9" tags a line span; the opener wants the file itself.
  const span = /:(\d+-\d+)$/.exec(raw);
  const path = span ? raw.slice(0, span.index) : raw;
  return {
    kind: "file",
    path: path.replace(/\/$/, ""),
    isDir: path.endsWith("/"),
    ...(span ? { lineSpan: span[1] } : {}),
  };
}
