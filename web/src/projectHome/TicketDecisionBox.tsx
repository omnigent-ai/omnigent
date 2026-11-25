// The open needs-human ask, at the top of a ticket: the question, who asked and
// when, its options as buttons, a way into the comment box, and the ask's evidence.
// Answering posts an ordinary comment, which the server routes as the answer.

import { useQuery } from "@tanstack/react-query";
import { PlayIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { personName } from "@/components/chat/TicketUpdateCards";
import { SessionImage } from "@/components/SessionImage";
import {
  listTicketEvents,
  type TicketPendingSignOff,
  type TicketWaitingOn,
} from "@/lib/projectTicketsApi";
import { cn } from "@/lib/utils";
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

/** A one-click answer: its short label, and the rest of its text for the button's detail. */
export interface DecisionOption {
  label: string;
  detail: string;
}

function splitOption(marker: string, text: string): DecisionOption {
  // "**Factory only** (recommended). The Lead files…" reads as "Factory only (recommended)".
  const full = plain(text);
  const [head = "", ...rest] = full.split(/(?<=[a-z0-9)])\. |: | — /i);
  const short = head.replace(/\.$/, "");
  const label = `${marker} ${short.length > OPTION_MAX_LENGTH ? `${short.slice(0, OPTION_MAX_LENGTH - 1)}…` : short}`;
  const detail = full
    .slice(head.length)
    .replace(/^(?:\.|:| —)\s*/, "")
    .trim();
  return { label, detail: rest.length ? detail : "" };
}

// "A. …", "B) …", "**C.** …": a lettered choice.
const LETTERED = /^\s*(?:[-*]\s+)?(?:\*\*)?([A-H])(?:\*\*)?[.):](?:\*\*)?\s+(.+)$/gm;
// "Option 1: …", "**Option B** — …": a named choice.
const NAMED =
  /^\s*(?:[-*]\s+)?(?:\*\*)?Option\s+([A-H]|\d{1,2})(?:\*\*)?\s*[.):—-]?(?:\*\*)?\s+(.+)$/gim;
const NUMBERED = /^\s*(\d+)[.)]\s+(.+)$/gm;
// Numbered items are choices only when the ask says to answer with a number.
const REPLY_WITH_NUMBER = /\b(?:reply|answer|respond|pick|choose)\b[^\n]*\b\d\b/i;

/**
 * The choices a question lists explicitly, as buttons: two or more lettered ("A. …") or
 * named ("Option 1: …") options, numbered items when the ask says to reply with a number,
 * or a yes-or-no question. A numbered plan or list of steps has no buttons; nothing is
 * ever guessed.
 */
export function decisionOptions(question: string): DecisionOption[] {
  return decisionChoices(question).options;
}

/** The buttons a question offers, and the lines of the ask they were read from. */
function decisionChoices(question: string): { options: DecisionOption[]; lines: string[] } {
  const asks = question.includes("?") || REPLY_WITH_NUMBER.test(question);
  const pick = (pattern: RegExp, marker: (key: string) => string) => {
    const seen = new Set<string>();
    const options: DecisionOption[] = [];
    const lines: string[] = [];
    for (const [line, key = "", text = ""] of question.matchAll(pattern)) {
      const option = splitOption(marker(key), text);
      lines.push(line);
      if (seen.has(option.label)) continue;
      seen.add(option.label);
      options.push(option);
    }
    return { options, lines };
  };
  if (asks) {
    for (const choices of [
      pick(NAMED, (key) => `Option ${key.toUpperCase()}:`),
      pick(LETTERED, (key) => `${key.toUpperCase()}.`),
      REPLY_WITH_NUMBER.test(question) ? pick(NUMBERED, (key) => `${key}.`) : null,
    ])
      if (choices && choices.options.length >= 2) return choices;
  }
  if (/\byes\s*(?:or|\/)\s*no\b/i.test(question))
    return {
      options: [
        { label: "Yes", detail: "" },
        { label: "No", detail: "" },
      ],
      lines: [],
    };
  return { options: [], lines: [] };
}

/**
 * The question to show: its text without embedded media. When the choices became
 * buttons, the lines they came from and a "Reply with 1 or 2" line are left out; other
 * lists, such as a numbered plan beside lettered choices, stay. Each button keeps its
 * option's full text, and "See the full ask" still shows every detail.
 */
export function questionStem(question: string, hasOptions: boolean): string {
  const text = splitTicketImages(question)
    .map((segment) => (segment.kind === "markdown" ? segment.text : ""))
    .join("\n");
  if (!hasOptions) return text.trim();
  const optionLines = new Set(decisionChoices(text).lines.map((line) => line.trim()));
  return text
    .split("\n")
    .filter((line) => !optionLines.has(line.trim()) && !/^\s*reply with\b/i.test(line))
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

const RECOMMENDATION =
  /^\s*(?:[-*]\s+)?(?:\*\*)?(?:I(?:'d| would)?\s+recommend|Recommend(?:ed|ation)?)\b(?:\*\*)?[:\s]+(.+)$/im;

/**
 * The ask's recommendation line ("I recommend A: it matches the web view."), its text,
 * and which option it picks, when it names one by its letter or number.
 */
export function askRecommendation(
  question: string,
  options: readonly DecisionOption[],
): { line: string; text: string; label: string | null } | null {
  const match = RECOMMENDATION.exec(question);
  if (!match) return null;
  const text = plain(match[1] ?? "");
  const key = /^(?:option\s+)?([A-H]|\d{1,2})\b/i.exec(text)?.[1]?.toUpperCase();
  const label =
    (key &&
      options.find(
        (option) =>
          option.label.startsWith(`${key}. `) || option.label.startsWith(`Option ${key}:`),
      )?.label) ||
    null;
  return { line: match[0], text, label };
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
  signOffOffered = false,
  requestedBy = null,
  pendingSignOffs = [],
  onAnswer,
  onAnswerInComment,
  onShowAsk,
}: {
  /** Who the work is for, named in the box as "for …". */
  requestedBy?: string | null;
  /** Sign-offs the ticket still owes, shown as their own row beside a question. */
  pendingSignOffs?: TicketPendingSignOff[];
  projectId: string;
  ticketId: string;
  waitingOn: TicketWaitingOn;
  viewerId: string | null;
  personaLookup: ReturnType<typeof useProjectPersonaLookup>;
  nowMs?: number;
  answering: boolean;
  /** The viewer can sign off right above; a "Yes" here wouldn't add the label the gate needs. */
  signOffOffered?: boolean;
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

  const isQuestion = waitingOn.ask_kind === "question";
  // A sign-off ask is answered by its label; a plain question keeps its choices.
  const options = signOffOffered && !isQuestion ? [] : decisionOptions(askText || question);
  const evidence = askEvidence(askText);
  const recommendation = askRecommendation(askText || question, options);
  let questionText = questionStem(question, options.length > 0);
  if (recommendation)
    questionText = questionText
      .split("\n")
      .filter((line) => line.trim() !== recommendation.line.trim())
      .join("\n")
      .trim();
  const forWhom = !requestedBy ? null : requestedBy === viewerId ? "you" : personName(requestedBy);
  const signOffRows = isQuestion ? pendingSignOffs : [];
  return (
    <section
      role="region"
      aria-label={isQuestion ? "Question for you" : "Decision needed"}
      data-testid="ticket-decision"
      className="space-y-2.5 rounded-lg border border-brand-accent/30 bg-brand-accent/[0.06] p-3 dark:bg-brand-accent/10"
    >
      <p className="text-xs">
        <span className="font-medium text-brand-accent">
          {isQuestion ? "Question for you" : "Decision needed"}
        </span>
        <span className="text-muted-foreground">
          {asker ? ` · ${isQuestion ? "from" : "asked by"} ${asker}` : ""}
          {isQuestion && forWhom && forWhom !== asker ? `, for ${forWhom}` : ""}
          {typeof waitingOn.raised_at === "number"
            ? ` · ${askedAgo(waitingOn.raised_at, nowMs)}`
            : ""}
        </span>
      </p>
      {questionText ? (
        <TicketMarkdown className="text-[15px] font-semibold text-foreground [&_li]:font-semibold [&_p]:font-semibold">
          {questionText}
        </TicketMarkdown>
      ) : (
        <p className="text-[15px] font-medium text-foreground">
          This ticket needs your response; see the latest comment.
        </p>
      )}
      {recommendation && (
        <p className="text-sm text-muted-foreground" data-testid="ticket-decision-recommendation">
          Recommended: {recommendation.text}
        </p>
      )}
      {signOffOffered && !isQuestion && (
        <p className="text-sm text-muted-foreground" data-testid="ticket-decision-sign-off">
          Approve or decline with the sign-off above; that adds the label landing needs.
        </p>
      )}
      {options.length > 0 && (
        <div className="flex flex-col gap-1.5" data-testid="ticket-decision-options">
          {options.map((option) => {
            const recommended = option.label === recommendation?.label;
            return (
              <Button
                key={option.label}
                type="button"
                size="sm"
                variant="outline"
                title={option.detail ? `${option.label}: ${option.detail}` : undefined}
                className={cn(
                  "mobile-touch-target h-auto min-h-8 w-full justify-start gap-2 py-1.5 text-left whitespace-normal",
                  recommended && "border-brand-accent/60",
                )}
                disabled={answering}
                onClick={() => onAnswer(`Decision: ${option.label}`)}
              >
                <span className="flex min-w-0 flex-1 flex-col items-start gap-0.5">
                  <span>{option.label}</span>
                  {option.detail && (
                    <span className="line-clamp-2 text-xs font-normal text-muted-foreground">
                      {option.detail}
                    </span>
                  )}
                </span>
                {recommended && (
                  <span className="shrink-0 text-xs font-medium text-brand-accent">
                    Recommended
                  </span>
                )}
              </Button>
            );
          })}
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
          {isQuestion ? "Or reply in your own words" : "Answer in a comment"}
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
      {signOffRows.map((signOff) => (
        <SignOffRow key={signOff.key} signOff={signOff} viewerId={viewerId} />
      ))}
    </section>
  );
}

const SIGN_OFF_NAMES: Record<string, string> = {
  "product-ok": "a product OK",
  "risk-ok": "a risk OK",
  "owner-ok": "the owner's OK",
};

/** A sign-off the ticket also owes, addressed to its approvers and not to the viewer. */
function SignOffRow({
  signOff,
  viewerId,
}: {
  signOff: TicketPendingSignOff;
  viewerId: string | null;
}) {
  const mine = viewerId !== null && signOff.approvers.includes(viewerId);
  const others = signOff.approvers.filter((user) => user !== viewerId).map(personName);
  const names =
    others.length > 1 ? `${others.slice(0, -1).join(", ")} or ${others.at(-1)}` : others[0];
  return (
    <div
      data-testid="ticket-decision-sign-off-row"
      className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 border-t border-brand-accent/20 pt-2 text-sm"
    >
      <span className="font-medium">Also needs {SIGN_OFF_NAMES[signOff.key] ?? signOff.key}</span>
      <span className="text-muted-foreground">
        {mine
          ? "You can give it with the sign-off above."
          : names
            ? `from ${names}. Not you.`
            : "Not you."}
      </span>
    </div>
  );
}
