// Quote Reply's text: a comment quoted into a reply draft, after whatever is already written.

/** Where Quote Reply puts its text: the box it opens, or why it can't right now. */
export interface CommentQuoteDestination {
  /** Set while the box can't take a quote, e.g. while its reply posts. */
  blockedReason?: string;
  quote: (body: string) => void;
}

/** `body` as a Markdown quote, one `> ` per line. */
export function quoteMarkdown(body: string): string {
  return body
    .replace(/\r\n?/g, "\n")
    .replace(/\n+$/, "")
    .split("\n")
    .map((line) => (line ? `> ${line}` : ">"))
    .join("\n");
}

/**
 * The reply draft once `body` is quoted into it: after what's already written, a blank line
 * apart, and with a blank line after the quote for the reply itself. Nothing written is lost.
 */
export function withQuotedReply(draft: string, body: string): string {
  const written = draft.replace(/\s+$/, "");
  return `${written ? `${written}\n\n` : ""}${quoteMarkdown(body)}\n\n`;
}
