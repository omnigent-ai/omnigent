import { createContext, useContext, useId, useMemo, useState } from "react";
import { parseMarkdownIntoBlocks } from "streamdown";
import { Button } from "@/components/ui/button";
import { markdownPreviewText } from "@/lib/markdownPreviewText";
import { PersonaAvatar } from "@/components/PersonaAvatar";
import { chipMentions, type MentionForm, type PersonaMention } from "./personaMentions";
import { TicketMarkdown } from "./ticketImages";

const PREVIEW_CHARACTERS = 600;
const NO_MENTIONS: PersonaMention[] = [];
const NO_FORMS: MentionForm[] = [];

/** The open ticket's project `@` forms, so its comments render mentions as pills. */
export const MentionFormsContext = createContext<readonly MentionForm[]>(NO_FORMS);
const PREVIEW_LINES = 8;

/** A presentation excerpt, never a replacement for the author's full comment. */
function commentPreview(body: string): { text: string; markdown: boolean } | null {
  if (body.length <= 800 && body.split(/\r?\n/).length <= 12) return null;

  const blocks = parseMarkdownIntoBlocks(body);
  const kept: string[] = [];
  for (const block of blocks) {
    const candidate = kept.join("") + block;
    if (candidate.length > PREVIEW_CHARACTERS || candidate.split(/\r?\n/).length > PREVIEW_LINES)
      break;
    kept.push(block);
  }
  // A section label without its content is not a useful excerpt.
  while (kept.length) {
    const last = kept.at(-1)!.trim();
    if (last && !/^(?:#{1,6}\s+[^\n]+|\*\*[^\n]+\*\*|__[^\n]+__)$/.test(last)) break;
    kept.pop();
  }
  const excerpt = kept.join("").trim();
  // Reference definitions can live beyond the excerpt; don't break their links or footnotes.
  const hasReferences = blocks.some((block) => /^ {0,3}\[[^\]\n]+\]:/m.test(block));
  if (excerpt && !hasReferences) return { text: excerpt, markdown: true };

  const plain = markdownPreviewText(body);
  const text =
    plain.length > PREVIEW_CHARACTERS
      ? `${plain
          .slice(0, PREVIEW_CHARACTERS)
          .replace(/\s+\S*$/, "")
          .trimEnd()}…`
      : plain;
  return { text: text || "…", markdown: false };
}

export function TicketCommentBody({
  body: text,
  mentions = NO_MENTIONS,
  mentionForms,
}: {
  body: string;
  mentions?: PersonaMention[];
  /** The project's `@` forms; each one the text uses renders as a pill. */
  mentionForms?: readonly MentionForm[];
}) {
  const [expanded, setExpanded] = useState(false);
  const contentId = useId();
  // A recorded mention still renders when the project's forms haven't loaded yet.
  const projectForms = useContext(MentionFormsContext);
  const known = mentionForms ?? projectForms;
  const forms = useMemo(
    () => [...known, ...mentions.map((m) => ({ form: m.name, label: m.name }))],
    [known, mentions],
  );
  const body = useMemo(() => chipMentions(text, forms), [text, forms]);
  const preview = useMemo(() => commentPreview(body), [body]);
  const visible = !expanded && preview ? preview : { text: body, markdown: true };
  return (
    <div className="space-y-1">
      <div id={contentId}>
        {visible.markdown ? (
          <TicketMarkdown key={expanded ? "full" : "preview"} className="text-ui leading-relaxed">
            {visible.text}
          </TicketMarkdown>
        ) : (
          <p className="text-ui leading-relaxed wrap-anywhere">{visible.text}</p>
        )}
      </div>
      {mentions.length > 0 && (
        <p className="flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
          cc
          {mentions.map((mention) => (
            <span
              key={mention.id}
              data-testid="ticket-comment-mention"
              className="inline-flex items-center gap-1 rounded-full bg-primary/10 py-0.5 pr-1.5 pl-0.5 font-medium text-primary"
            >
              <PersonaAvatar name={mention.name} size="xs" />@{mention.name}
            </span>
          ))}
        </p>
      )}
      {preview && (
        <Button
          type="button"
          variant="link"
          size="sm"
          className="h-auto px-0 py-1 text-xs text-muted-foreground hover:text-foreground"
          aria-controls={contentId}
          aria-expanded={expanded}
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? "Show less" : "Show more"}
        </Button>
      )}
    </div>
  );
}
