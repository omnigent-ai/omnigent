import { useId, useMemo, useState } from "react";
import { parseMarkdownIntoBlocks } from "streamdown";
import { Button } from "@/components/ui/button";
import { markdownPreviewText } from "@/lib/markdownPreviewText";
import { TicketMarkdown } from "./ticketImages";

const PREVIEW_CHARACTERS = 600;
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

export function TicketCommentBody({ body }: { body: string }) {
  const [expanded, setExpanded] = useState(false);
  const contentId = useId();
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
