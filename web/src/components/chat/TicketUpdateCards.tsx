// Shows an automatic ticket-update turn as one quiet card: a single ticket, or a
// grouped list for a digest. The agent still receives the original text.

import { useContext, type ReactNode } from "react";
import { useInRouterContext } from "react-router-dom";
import { Link } from "@/lib/routing";
import { QueryClientContext, useQuery } from "@tanstack/react-query";
import { ArrowUpRightIcon, BotIcon, CircleAlertIcon } from "lucide-react";
import { useProjects } from "@/hooks/useConversations";
import { getTicket, getTicketStages, type TicketStage } from "@/lib/projectTicketsApi";
import type { TicketUpdateCard, TicketUpdateMessage } from "@/lib/ticketUpdateMessage";
import { userColor, userInitials } from "@/lib/userBadge";
import { cn } from "@/lib/utils";
import { projectHomePath, ticketPanelSearch } from "@/projectHome/projectHomeNavigation";
import { TicketStageBadge } from "@/projectHome/TicketStageAppearance";

// Transcript cards read the board's cache; they don't poll like the board does.
const CARD_STALE_MS = 30_000;

/** "corey.zumar@x.com" → "Corey Zumar". */
export function personName(userId: string): string {
  const local = userId.split("@", 1)[0] ?? userId;
  const words = local.split(/[._\-+]+/).filter(Boolean);
  if (!words.length) return userId;
  return words.map((word) => word.charAt(0).toUpperCase() + word.slice(1)).join(" ");
}

/** The question a needs-human label asks, without a leading "Name:" address. */
function askOf(card: TicketUpdateCard): string | null {
  const label = card.labels?.find((entry) => entry.key === "needs-human");
  if (!label) return null;
  const value = (label.value ?? "").trim().replace(/^[A-Z][\w .-]{0,30}:\s+/, "");
  return value ? value.charAt(0).toUpperCase() + value.slice(1) : "A person's answer is needed.";
}

const OUTPUT_DETAIL = /^(?:artifact|link|pull_request) /;

/** One digest part in plain words, e.g. ("agent note", 2) → "2 agent notes". */
function digestPart(name: string, count: number): string {
  if (name === "output") return count > 1 ? `${count} files added` : "added a file";
  if (name === "agent note" || name === "execution note")
    return count > 1 ? `${count} ${name}s` : name;
  const plain =
    name === "edited labels"
      ? "labels updated"
      : name.replace(
          /^edited (.+)$/,
          (_, fields: string) => `edited ${fields.replaceAll("/", ", ")}`,
        );
  return count > 1 ? `${plain} ×${count}` : plain;
}

/** A digest line's change summary in plain words, e.g. "agent note ×2" → "2 agent notes". */
function digestPhrase(summary: string): string {
  const parts = summary
    .replaceAll("person's comment, answered by its worker", "comment answered")
    .replaceAll("stage_id", "stage")
    .replaceAll("_", " ")
    .split(", ")
    .map((part) => {
      const counted = /^(.+?) ×(\d+)$/.exec(part);
      return counted ? digestPart(counted[1]!, Number(counted[2])) : digestPart(part, 1);
    });
  const text = parts.join(", ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** What happened, in a few words, beside the stage chip. */
function eventPhrase(card: TicketUpdateCard): string | null {
  const comment = card.comment;
  if (card.created) return "New ticket";
  if (comment?.author) return "New comment";
  if (comment?.final) return "Final update";
  if (card.labels?.some((label) => label.key === "approved")) return "Approved";
  if (card.outputs.length)
    return card.outputs.length === 1 ? "Added a file" : `Added ${card.outputs.length} files`;
  if (card.editedFields.length) return `Edited ${card.editedFields.join(", ")}`;
  if (card.labels && !card.movedTo && !comment) return "Labels updated";
  if (card.digestSummary) return digestPhrase(card.digestSummary);
  return null;
}

function Avatar({ author }: { author: string | null }) {
  if (!author)
    return (
      <span className="mt-0.5 grid size-4 shrink-0 place-items-center rounded-full bg-muted text-muted-foreground">
        <BotIcon aria-hidden className="size-2.5" />
      </span>
    );
  return (
    <span
      aria-hidden
      className="mt-0.5 grid size-4 shrink-0 place-items-center rounded-full text-[8px] font-semibold text-white"
      style={{ backgroundColor: userColor(author) }}
    >
      {userInitials(author)}
    </span>
  );
}

function Quote({ card, oneLine }: { card: TicketUpdateCard; oneLine: boolean }) {
  const text = card.comment?.text ?? card.digestLatest;
  if (!text) return null;
  const author = card.comment?.author ?? null;
  return (
    <div className="mt-2 flex min-w-0 items-start gap-1.5 border-l-2 pl-2.5 text-[13px] leading-5 text-muted-foreground">
      <Avatar author={author} />
      <p className={cn("min-w-0 break-words", oneLine ? "line-clamp-1" : "line-clamp-2")}>
        {author && <span className="font-medium text-foreground">{personName(author)} </span>}
        {text}
      </p>
    </div>
  );
}

function StageLine({ card, stage }: { card: TicketUpdateCard; stage: TicketStage | null }) {
  const phrase = eventPhrase(card);
  const moved = card.movedTo !== null && !card.created;
  if (!stage && !phrase) return null;
  return (
    <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-1 text-[13px] leading-5 text-muted-foreground">
      {moved && <span>Moved to</span>}
      {stage && <TicketStageBadge stage={stage} className="h-[18px] px-1.5 text-xs" />}
      {phrase && !(moved && !card.comment && !card.labels) && <span>{phrase}</span>}
    </div>
  );
}

/** Opens the ticket when it can; otherwise a plain block. */
function TicketLink({
  href,
  title,
  className,
  children,
}: {
  href: string | null;
  title: string;
  className: string;
  children: ReactNode;
}) {
  const inRouter = useInRouterContext();
  if (href && inRouter)
    return (
      <Link
        to={href}
        data-testid="ticket-update-card"
        aria-label={`Open ticket: ${title}`}
        className={cn(className, "group/ticket-card transition-colors hover:bg-muted/60")}
      >
        {children}
      </Link>
    );
  return (
    <div data-testid="ticket-update-card" className={className}>
      {children}
    </div>
  );
}

function OpenHint() {
  return (
    <ArrowUpRightIcon
      aria-hidden
      className="mt-0.5 size-3.5 shrink-0 text-muted-foreground opacity-60 transition-opacity [@media(hover:hover)]:opacity-0 group-hover/ticket-card:opacity-100 group-focus-visible/ticket-card:opacity-100"
    />
  );
}

interface CardView {
  card: TicketUpdateCard;
  title: string;
  stage: TicketStage | null;
  href: string | null;
}

/** The full card for a turn about one ticket. */
function SingleCard({ view }: { view: CardView }) {
  const { card, title, stage, href } = view;
  const ask = askOf(card);
  const attention = ask !== null || card.escalated;
  return (
    <div
      className={cn(
        "overflow-hidden rounded-xl border bg-background shadow-xs dark:bg-white/[0.025]",
        attention && "border-warning/50",
      )}
    >
      {attention && (
        <div className="flex items-center gap-1.5 bg-warning/12 px-3.5 py-1.5 text-xs font-medium text-warning">
          <CircleAlertIcon aria-hidden className="size-3.5" />
          {ask ? "Needs your answer" : "No one has replied yet"}
        </div>
      )}
      <TicketLink href={href} title={title} className="block px-3.5 pt-2.5 pb-3">
        <div className="flex min-w-0 items-start gap-2">
          <span className="line-clamp-2 min-w-0 flex-1 text-sm font-medium text-pretty text-foreground">
            {title}
          </span>
          {href && <OpenHint />}
        </div>
        <StageLine card={card} stage={stage} />
        {ask && <p className="mt-2 text-sm font-medium text-pretty text-foreground">{ask}</p>}
        <Quote card={card} oneLine={ask !== null} />
      </TicketLink>
    </div>
  );
}

/** One compact row of a multi-ticket turn. */
function DigestRow({ view }: { view: CardView }) {
  const { card, title, stage, href } = view;
  const ask = askOf(card);
  const latest =
    card.digestLatest && !OUTPUT_DETAIL.test(card.digestLatest) ? card.digestLatest : null;
  const detail = (ask ?? card.comment?.text ?? latest ?? eventPhrase(card))?.replace(/\.$/, "");
  return (
    <TicketLink
      href={href}
      title={title}
      className="flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1 px-3.5 py-2 sm:flex-nowrap"
    >
      <span className="order-first line-clamp-2 min-w-0 basis-full text-sm font-medium text-foreground sm:order-none sm:line-clamp-1 sm:basis-auto sm:flex-1 sm:break-all">
        {title}
      </span>
      {stage && (
        <span className="sm:order-first">
          <TicketStageBadge stage={stage} className="h-[18px] px-1.5 text-xs" />
        </span>
      )}
      {detail && (
        <span
          className={cn(
            "min-w-0 flex-1 truncate text-[13px] sm:max-w-[45%] sm:flex-none",
            ask || card.escalated ? "font-medium text-warning" : "text-muted-foreground",
          )}
        >
          {ask ? `Needs your answer: ${ask}` : detail}
        </span>
      )}
    </TicketLink>
  );
}

function useCardView(projectId: string, card: TicketUpdateCard): CardView {
  const ticket = useQuery({
    queryKey: ["project-tickets", projectId, card.ticketId],
    queryFn: () => getTicket(projectId, card.ticketId),
    staleTime: CARD_STALE_MS,
    retry: false,
  });
  const stages = useQuery({
    queryKey: ["project-tickets", projectId, "stages", false],
    queryFn: () => getTicketStages(projectId),
    staleTime: CARD_STALE_MS,
    retry: false,
    enabled: card.movedTo === null,
  });
  const projects = useProjects();
  const projectName = projects.data?.find((project) => project.id === projectId)?.name ?? null;
  const currentStage =
    stages.data?.stages.find((stage) => stage.id === ticket.data?.stage_id) ?? null;
  return {
    card,
    title: ticket.data?.title ?? card.title ?? "Ticket",
    stage: card.movedTo ?? currentStage,
    href: projectName ? `${projectHomePath(projectName)}${ticketPanelSearch(card.ticketId)}` : null,
  };
}

function offlineView(card: TicketUpdateCard): CardView {
  return { card, title: card.title ?? "Ticket", stage: card.movedTo, href: null };
}

function LiveSingle({ projectId, card }: { projectId: string; card: TicketUpdateCard }) {
  return <SingleCard view={useCardView(projectId, card)} />;
}

function LiveRow({ projectId, card }: { projectId: string; card: TicketUpdateCard }) {
  return <DigestRow view={useCardView(projectId, card)} />;
}

export function TicketUpdateCards({ message }: { message: TicketUpdateMessage }) {
  // Isolated renders (tests, previews) have no query client: show what the text holds.
  const live = useContext(QueryClientContext) !== undefined;
  const { cards, projectId } = message;
  if (cards.length === 1) {
    const card = cards[0]!;
    return (
      <div data-testid="ticket-update-cards" className="w-full max-w-[640px]">
        {live ? (
          <LiveSingle projectId={projectId} card={card} />
        ) : (
          <SingleCard view={offlineView(card)} />
        )}
      </div>
    );
  }
  const attention = cards.some((card) => askOf(card) !== null || card.escalated);
  return (
    <div
      data-testid="ticket-update-cards"
      className={cn(
        "w-full max-w-[640px] overflow-hidden rounded-xl border bg-background shadow-xs dark:bg-white/[0.025]",
        attention && "border-warning/50",
      )}
    >
      <div
        className={cn(
          "flex items-center gap-1.5 border-b px-3.5 py-1.5 text-xs font-medium",
          attention ? "border-transparent bg-warning/12 text-warning" : "text-muted-foreground",
        )}
      >
        {attention && <CircleAlertIcon aria-hidden className="size-3.5" />}
        {cards.length} tickets updated{attention ? " · needs your answer" : ""}
      </div>
      <div className="divide-y">
        {cards.map((card) =>
          live ? (
            <LiveRow key={card.ticketId} projectId={projectId} card={card} />
          ) : (
            <DigestRow key={card.ticketId} view={offlineView(card)} />
          ),
        )}
      </div>
    </div>
  );
}
