// The open needs-human ask, at the top of a ticket: the question, who asked and
// when, its options as buttons, a way into the comment box, and the ask's evidence.
// Answering posts an ordinary comment, which the server routes as the answer.

import { useQuery } from "@tanstack/react-query";
import { PlayIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { personName } from "@/components/chat/TicketUpdateCards";
import { SessionImage } from "@/components/SessionImage";
import { listTicketEvents, type TicketWaitingOn } from "@/lib/projectTicketsApi";
import { relativeTime } from "@/lib/relativeTime";
import { TicketMarkdown, splitTicketImages } from "./ticketImages";
import { needsWhom } from "./ticketWaitingOn";
import { type useProjectPersonaLookup, usePersonaForSession } from "./usePersonaSessions";

/** Most evidence thumbnails the box shows; the rest stay in the activity. */
const EVIDENCE_LIMIT = 3;
const OPTION_MAX_LENGTH = 60;

function plain(markdown: string): string {
  return markdown
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[*_`#>]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function askedAgo(raisedAtSeconds: number, nowMs: number): string {
  const ago = relativeTime(raisedAtSeconds * 1000, nowMs);
  return ago === "now" ? "just now" : `${ago} ago`;
}

function optionLabel(text: string): string {
  // "**Factory only** (recommended). The Lead files…" reads as "Factory only (recommended)".
  const head = (plain(text).split(/(?<=[a-z0-9)])\. |: | — /i)[0] ?? "").replace(/\.$/, "");
  return head.length > OPTION_MAX_LENGTH ? `${head.slice(0, OPTION_MAX_LENGTH - 1)}…` : head;
}

/**
 * The choices a question lists explicitly: two or more numbered items ("1. …", "2) …"),
 * or a yes-or-no question. Anything else has no buttons; it is never guessed.
 */
export function decisionOptions(question: string): string[] {
  const numbered = [...question.matchAll(/^\s*(\d+)[.)]\s+(.+)$/gm)].map(
    ([, number, text]) => `${number}. ${optionLabel(text ?? "")}`,
  );
  if (numbered.length >= 2) return [...new Set(numbered)];
  if (/\byes\s*(?:or|\/)\s*no\b/i.test(question)) return ["Yes", "No"];
  return [];
}

/**
 * The question to show: its text without embedded media. When the choices became
 * buttons, their list and a "Reply with 1 or 2" line are left out; "See the full ask"
 * still shows every detail.
 */
export function questionStem(question: string, hasOptions: boolean): string {
  const text = splitTicketImages(question)
    .map((segment) => (segment.kind === "markdown" ? segment.text : ""))
    .join("\n");
  if (!hasOptions) return text.trim();
  return text
    .split("\n")
    .filter((line) => !/^\s*\d+[.)]\s+/.test(line) && !/^\s*reply with\b/i.test(line))
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/** The images and videos embedded in the ask's comment, newest last. */
export function askEvidence(text: string) {
  return splitTicketImages(text)
    .filter((segment) => segment.kind !== "markdown")
    .slice(0, EVIDENCE_LIMIT) as { kind: "image" | "video"; alt: string; path: string }[];
}

function useAskComment(projectId: string, ticketId: string, position: number | null) {
  return useQuery({
    queryKey: ["project-tickets", projectId, ticketId, "ask-event", position],
    queryFn: async () => {
      const page = await listTicketEvents(projectId, ticketId, (position as number) - 1);
      return page.data.find((event) => event.position === position) ?? null;
    },
    enabled: position !== null,
    staleTime: 60_000,
  });
}

function Evidence({
  items,
  onShowAsk,
}: {
  items: ReturnType<typeof askEvidence>;
  onShowAsk: () => void;
}) {
  if (items.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-2" data-testid="ticket-decision-evidence">
      {items.map((item) =>
        item.kind === "image" ? (
          <SessionImage
            key={item.path}
            path={item.path}
            alt={item.alt}
            className="h-16 w-auto rounded-md border border-border object-cover"
          />
        ) : (
          <button
            key={item.path}
            type="button"
            onClick={onShowAsk}
            className="flex h-16 items-center gap-1.5 rounded-md border border-border bg-muted/40 px-3 text-xs text-muted-foreground hover:text-foreground"
            aria-label={`Play ${item.alt} in the ask`}
          >
            <PlayIcon aria-hidden className="size-4" />
            {item.alt}
          </button>
        ),
      )}
    </div>
  );
}

/**
 * The ticket's open ask. For someone it waits on: the full question, options as
 * buttons, "Answer in a comment" and the ask's evidence. For anyone else: one grey line.
 */
export function TicketDecisionBox({
  projectId,
  ticketId,
  waitingOn,
  viewerId,
  personaLookup,
  nowMs = Date.now(),
  answering,
  onAnswer,
  onAnswerInComment,
  onShowAsk,
}: {
  projectId: string;
  ticketId: string;
  waitingOn: TicketWaitingOn;
  viewerId: string | null;
  personaLookup: ReturnType<typeof useProjectPersonaLookup>;
  nowMs?: number;
  answering: boolean;
  onAnswer: (text: string) => void;
  onAnswerInComment: (mention: string) => void;
  onShowAsk: (position: number) => void;
}) {
  const whom = needsWhom(waitingOn, viewerId);
  const persona = usePersonaForSession(waitingOn.raised_by_session_id ?? null, personaLookup);
  const ask = useAskComment(projectId, ticketId, waitingOn.ask_position ?? null);
  const question = (waitingOn.question ?? "").trim();
  const asker =
    persona ??
    (waitingOn.raised_by_user_id
      ? personName(waitingOn.raised_by_user_id)
      : waitingOn.raised_by_session_id
        ? "an agent"
        : null);
  const askText = typeof ask.data?.payload.text === "string" ? ask.data.payload.text : question;
  const showAsk = () => {
    if (typeof waitingOn.ask_position === "number") onShowAsk(waitingOn.ask_position);
  };

  if (whom?.kind !== "you") {
    if (!question || !whom) return null;
    return (
      <p
        role="region"
        aria-label="Decision needed"
        data-testid="ticket-decision-waiting"
        className="line-clamp-2 rounded-md bg-muted/60 px-3 py-2 text-sm text-muted-foreground"
      >
        {whom.title}: {plain(question)}
      </p>
    );
  }

  const options = decisionOptions(askText || question);
  const evidence = askEvidence(askText);
  const questionText = questionStem(question, options.length > 0);
  return (
    <section
      role="region"
      aria-label="Decision needed"
      data-testid="ticket-decision"
      className="space-y-2.5 rounded-lg border border-brand-accent/30 bg-brand-accent/[0.06] p-3 dark:bg-brand-accent/10"
    >
      <p className="text-xs">
        <span className="font-medium text-brand-accent">Decision needed</span>
        <span className="text-muted-foreground">
          {asker ? ` · asked by ${asker}` : ""}
          {typeof waitingOn.raised_at === "number"
            ? ` · ${askedAgo(waitingOn.raised_at, nowMs)}`
            : ""}
        </span>
      </p>
      {questionText ? (
        <TicketMarkdown className="text-[15px] font-medium text-foreground [&_p]:font-medium [&_li]:font-medium">
          {questionText}
        </TicketMarkdown>
      ) : (
        <p className="text-[15px] font-medium text-foreground">
          This ticket needs your response; see the latest comment.
        </p>
      )}
      {options.length > 0 && (
        <div className="flex flex-wrap gap-2" data-testid="ticket-decision-options">
          {options.map((option) => (
            <Button
              key={option}
              type="button"
              size="sm"
              variant="outline"
              className="mobile-touch-target h-auto min-h-8 max-w-full shrink whitespace-normal text-left"
              disabled={answering}
              onClick={() => onAnswer(`Decision: ${option}`)}
            >
              {option}
            </Button>
          ))}
        </div>
      )}
      <Evidence items={evidence} onShowAsk={showAsk} />
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
        <Button
          type="button"
          variant="link"
          size="sm"
          className="h-auto px-0"
          data-testid="ticket-decision-comment"
          onClick={() => onAnswerInComment(persona ? `@${persona} ` : "")}
        >
          Answer in a comment
        </Button>
        {typeof waitingOn.ask_position === "number" && (
          <Button
            type="button"
            variant="link"
            size="sm"
            className="h-auto px-0 text-muted-foreground"
            onClick={showAsk}
          >
            See the full ask
          </Button>
        )}
      </div>
    </section>
  );
}
