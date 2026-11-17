// A tile's details, opened from its title: what the agent is doing, where,
// what it has cost, its latest reply and who it is. Fetches only while open.

import { useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { GitPullRequestIcon, PencilIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useRenameConversation, type Conversation } from "@/hooks/useConversations";
import { useSession } from "@/hooks/useSession";
import { getSessionState } from "@/hooks/useSessionState";
import { formatSessionCostUsd, formatTokenCount } from "@/lib/formatCost";
import { fetchLastAssistantText } from "@/lib/lastAssistantText";
import { getSessionUsage } from "@/lib/sessionsApi";
import { cn } from "@/lib/utils";
import { HeaderTitleInput } from "@/shell/HeaderTitle";
import { activityNotes, projectName, relativeAge, statusLabel, usageTotals } from "./tileSummary";

const DETAILS_STALE_MS = 30_000;
const REPLY_PREVIEW_CHARS = 280;

function DetailCard({
  title,
  wide = false,
  accent,
  children,
}: {
  title: string;
  wide?: boolean;
  accent?: string;
  children: ReactNode;
}) {
  return (
    <section
      aria-label={title}
      className={cn(
        "flex min-w-0 flex-col gap-1 rounded-md border bg-background/60 px-2.5 py-2",
        wide && "col-span-2",
      )}
      style={accent ? { borderLeftColor: accent, borderLeftWidth: 3 } : undefined}
    >
      <h3 className="font-semibold text-[11px] text-muted-foreground uppercase tracking-wide">
        {title}
      </h3>
      {children}
    </section>
  );
}

function Row({ label, value, mono = false }: { label: string; value: ReactNode; mono?: boolean }) {
  return (
    <div className="flex min-w-0 gap-2 text-xs">
      <span className="shrink-0 text-muted-foreground">{label}</span>
      <span className={cn("min-w-0 truncate text-foreground", mono && "font-mono")}>{value}</span>
    </div>
  );
}

/** The session's title with the header's inline rename; sub-agents keep their name. */
function TitleRow({ conversation, title }: { conversation: Conversation; title: string }) {
  const rename = useRenameConversation();
  const [editing, setEditing] = useState(false);
  const canRename = !conversation.parent_session_id;
  return (
    <div className="flex min-w-0 items-center gap-2 text-xs">
      <span className="shrink-0 text-muted-foreground">Title</span>
      {editing ? (
        <HeaderTitleInput
          initialTitle={title}
          pending={rename.isPending}
          onCommit={(next) => {
            const trimmed = next.trim();
            if (trimmed && trimmed !== title)
              rename.mutate({ id: conversation.id, title: trimmed });
            setEditing(false);
          }}
          onCancel={() => setEditing(false)}
        />
      ) : (
        <>
          <span className="min-w-0 truncate text-foreground">{title}</span>
          {canRename && (
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label={`Rename ${title}`}
              title="Rename"
              onClick={() => setEditing(true)}
            >
              <PencilIcon className="size-3" />
            </Button>
          )}
        </>
      )}
    </div>
  );
}

interface TileDetailsProps {
  id: string;
  conversation: Conversation;
  title: string;
  accent: string;
}

export function TileDetails({ id, conversation, title, accent }: TileDetailsProps) {
  const sessionId = conversation.id;
  const { session } = useSession(sessionId);
  const usage = useQuery({
    queryKey: ["tiles", "details", "usage", sessionId],
    queryFn: ({ signal }) => getSessionUsage(sessionId, { signal }),
    staleTime: DETAILS_STALE_MS,
  });
  const reply = useQuery({
    // A new activity time means there may be a newer reply.
    queryKey: ["tiles", "details", "reply", sessionId, conversation.updated_at],
    queryFn: async () => (await fetchLastAssistantText(sessionId, REPLY_PREVIEW_CHARS)) ?? null,
    staleTime: DETAILS_STALE_MS,
  });

  const notes = activityNotes(conversation);
  const totals = usageTotals(usage.data?.usageByModel ?? null);
  const cost = usage.data?.totalCostUsd ?? null;
  const model = session?.modelOverride ?? session?.llmModel ?? null;
  const pullRequest = conversation.pull_request;

  return (
    <div
      id={id}
      data-testid="tile-details"
      className="grid min-h-0 flex-1 auto-rows-min grid-cols-2 content-start gap-2 overflow-y-auto bg-muted/30 p-2"
    >
      <DetailCard title="Now" accent={accent}>
        <span className="font-medium text-sm">
          {statusLabel(getSessionState(conversation), conversation.status)}
        </span>
        {notes.map((note) => (
          <span key={note} className="text-xs">
            {note}
          </span>
        ))}
        <span className="text-muted-foreground text-xs">
          Last activity {relativeAge(conversation.updated_at)}
        </span>
      </DetailCard>

      <DetailCard title="Where">
        <Row label="Project" value={projectName(conversation.workspace) ?? "None"} />
        {conversation.git_branch && <Row label="Branch" value={conversation.git_branch} mono />}
        {conversation.workspace && (
          <span
            className="truncate font-mono text-[11px] text-muted-foreground"
            title={conversation.workspace}
          >
            {conversation.workspace}
          </span>
        )}
        {pullRequest && (
          <a
            href={pullRequest.url}
            target="_blank"
            rel="noreferrer"
            className="flex min-w-0 items-center gap-1 text-xs hover:underline"
          >
            <GitPullRequestIcon aria-hidden className="size-3.5 shrink-0" />
            <span className="truncate">
              #{pullRequest.number}
              {pullRequest.title ? ` ${pullRequest.title}` : ""}
            </span>
          </a>
        )}
      </DetailCard>

      <DetailCard title="Usage">
        {usage.isPending ? (
          <span className="text-muted-foreground text-xs">Loading…</span>
        ) : usage.isError ? (
          <span className="text-muted-foreground text-xs">Usage unavailable</span>
        ) : (
          <>
            <Row label="Cost" value={cost === null ? "Not priced" : formatSessionCostUsd(cost)} />
            <Row
              label="Tokens"
              value={
                totals
                  ? `${formatTokenCount(totals.total)} (${formatTokenCount(totals.input)} in · ${formatTokenCount(totals.output)} out)`
                  : "None yet"
              }
            />
          </>
        )}
      </DetailCard>

      <DetailCard title="Agent">
        <TitleRow conversation={conversation} title={title} />
        <Row label="Name" value={conversation.agent_name ?? "Agent"} />
        {conversation.harness && <Row label="Harness" value={conversation.harness} />}
        {model && <Row label="Model" value={model} mono />}
        <Row
          label="Started"
          value={new Date(conversation.created_at * 1000).toLocaleString(undefined, {
            dateStyle: "medium",
            timeStyle: "short",
          })}
        />
      </DetailCard>

      <DetailCard title="Latest reply" wide>
        {reply.isPending ? (
          <span className="text-muted-foreground text-xs">Loading…</span>
        ) : (
          <p className="whitespace-pre-line text-xs">
            {reply.data ?? <span className="text-muted-foreground">No reply yet.</span>}
          </p>
        )}
      </DetailCard>
    </div>
  );
}
