// Shows an automatic ticket-update turn as a short stack of ticket cards. The agent
// still receives the original text, which "Show raw" reveals.

import { useContext, useState } from "react";
import { useInRouterContext } from "react-router-dom";
import { Link } from "@/lib/routing";
import { QueryClientContext, useQuery } from "@tanstack/react-query";
import { ArrowUpRightIcon, MessageSquareIcon, TicketIcon } from "lucide-react";
import { useProjects } from "@/hooks/useConversations";
import {
  getTicket,
  getTicketStages,
  type TicketLabel,
  type TicketStage,
} from "@/lib/projectTicketsApi";
import type { TicketUpdateCard, TicketUpdateMessage } from "@/lib/ticketUpdateMessage";
import { formatMessageTimestamp } from "@/lib/relativeTime";
import { cn } from "@/lib/utils";
import { projectHomePath, ticketPanelSearch } from "@/projectHome/projectHomeNavigation";
import { TicketStageBadge } from "@/projectHome/TicketStageAppearance";

// Transcript cards read the board's cache; they don't poll like the board does.
const CARD_STALE_MS = 30_000;
const LABEL_VALUE_CHARS = 24;

function labelText(label: TicketLabel): string {
  const value = (label.value ?? "").trim();
  if (!value) return label.key;
  const shown = /^[0-9a-f]{40}$/.test(value)
    ? value.slice(0, 7)
    : value.length > LABEL_VALUE_CHARS
      ? `${value.slice(0, LABEL_VALUE_CHARS - 1)}…`
      : value;
  return `${label.key}: ${shown}`;
}

function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** The card's one-line "what changed" summary, without labels or the comment. */
function changePhrases(card: TicketUpdateCard): string[] {
  const phrases: string[] = [];
  if (card.created) phrases.push("Created");
  if (card.movedTo && !card.created) phrases.push(`Moved to ${card.movedTo.name}`);
  if (card.editedFields.length) phrases.push(`Edited ${card.editedFields.join(", ")}`);
  if (card.outputs.length)
    phrases.push(
      card.outputs.length === 1 ? "Added an output" : `Added ${card.outputs.length} outputs`,
    );
  if (!phrases.length && !card.labels && !card.comment && card.digestSummary)
    phrases.push(
      capitalize(card.digestSummary.replaceAll("stage_id", "stage").replaceAll("_", " ")),
    );
  return phrases;
}

function commentLead(card: TicketUpdateCard): string {
  const comment = card.comment!;
  const parts = [
    comment.final ? "Final update" : comment.kind === "execution" ? "Execution note" : "Comment",
  ];
  if (comment.audience === "human") parts.push("for people");
  else if (comment.audience === "agents") parts.push("agent note");
  if (comment.author) parts.push(`by ${comment.author}`);
  return parts.join(" · ");
}

function CardBody({
  card,
  title,
  stage,
  href,
}: {
  card: TicketUpdateCard;
  title: string;
  stage: TicketStage | null;
  href: string | null;
}) {
  const phrases = changePhrases(card);
  const snippet = card.comment?.text ?? card.digestLatest;
  const inRouter = useInRouterContext();
  const content = (
    <>
      <div className="flex min-w-0 items-center gap-2">
        {stage ? (
          <TicketStageBadge stage={stage} />
        ) : (
          <TicketIcon aria-hidden className="size-3.5 shrink-0 text-muted-foreground" />
        )}
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-foreground">{title}</span>
        {href && (
          <ArrowUpRightIcon
            aria-hidden
            className="size-3.5 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover/ticket-card:opacity-100"
          />
        )}
      </div>
      {(phrases.length > 0 || card.labels?.length || card.escalated) && (
        <div className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-muted-foreground">
          {phrases.length > 0 && <span>{phrases.join(" · ")}</span>}
          {card.labels && card.labels.length > 0 && (
            <>
              {phrases.length > 0 && <span aria-hidden>·</span>}
              <span>Labels</span>
              {card.labels.map((label) => (
                <span
                  key={label.key}
                  title={label.value ? `${label.key}: ${label.value}` : label.key}
                  className={cn(
                    "max-w-full truncate rounded px-1.5 py-px",
                    label.key === "needs-human"
                      ? "bg-warning/15 font-medium text-warning"
                      : "bg-muted text-muted-foreground",
                  )}
                >
                  {labelText(label)}
                </span>
              ))}
            </>
          )}
          {card.escalated && (
            <span className="rounded bg-warning/15 px-1.5 py-px font-medium text-warning">
              No worker replied
            </span>
          )}
        </div>
      )}
      {snippet && (
        <div className="flex min-w-0 items-start gap-1.5 text-xs text-muted-foreground">
          <MessageSquareIcon aria-hidden className="mt-0.5 size-3 shrink-0" />
          <p className="line-clamp-2 min-w-0 break-words">
            {card.comment && (
              <span className="font-medium text-foreground/80">{commentLead(card)}: </span>
            )}
            {snippet}
          </p>
        </div>
      )}
    </>
  );
  const className =
    "group/ticket-card flex min-w-0 flex-col gap-1 rounded-lg border bg-card px-3 py-2 text-left";
  if (href && inRouter)
    return (
      <Link
        to={href}
        data-testid="ticket-update-card"
        aria-label={`Open ticket: ${title}`}
        className={cn(className, "transition-colors hover:bg-muted/50")}
      >
        {content}
      </Link>
    );
  return (
    <div data-testid="ticket-update-card" className={className}>
      {content}
    </div>
  );
}

/** A card that also reads the ticket's current title and stage, and links to it. */
function LiveTicketCard({ projectId, card }: { projectId: string; card: TicketUpdateCard }) {
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
  return (
    <CardBody
      card={card}
      title={ticket.data?.title ?? card.title ?? "Ticket"}
      stage={card.movedTo ?? currentStage}
      href={
        projectName ? `${projectHomePath(projectName)}${ticketPanelSearch(card.ticketId)}` : null
      }
    />
  );
}

export function TicketUpdateCards({
  message,
  rawText,
  createdAtS,
}: {
  message: TicketUpdateMessage;
  rawText: string;
  createdAtS?: number;
}) {
  const [showRaw, setShowRaw] = useState(false);
  // Isolated renders (tests, previews) have no query client: show what the text holds.
  const live = useContext(QueryClientContext) !== undefined;
  const count = message.cards.length;
  return (
    <div data-testid="ticket-update-cards" className="flex w-full max-w-[640px] flex-col gap-1.5">
      <div className="flex items-center gap-1.5 px-1 text-xs text-muted-foreground">
        <TicketIcon aria-hidden className="size-3.5" />
        <span>
          Ticket update{count > 1 ? ` · ${count} tickets` : ""}
          {createdAtS ? ` · ${formatMessageTimestamp(createdAtS)}` : ""}
        </span>
        <button
          type="button"
          onClick={() => setShowRaw(!showRaw)}
          aria-expanded={showRaw}
          className="ml-auto rounded px-1 hover:bg-muted hover:text-foreground"
        >
          {showRaw ? "Hide raw" : "Show raw"}
        </button>
      </div>
      {message.cards.map((card) =>
        live ? (
          <LiveTicketCard key={card.ticketId} projectId={message.projectId} card={card} />
        ) : (
          <CardBody
            key={card.ticketId}
            card={card}
            title={card.title ?? "Ticket"}
            stage={card.movedTo}
            href={null}
          />
        ),
      )}
      {showRaw && (
        <pre
          data-testid="ticket-update-raw"
          className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-muted px-3 py-2 text-xs text-muted-foreground"
        >
          {rawText}
        </pre>
      )}
    </div>
  );
}
