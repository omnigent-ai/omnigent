import { useState } from "react";
import { ArrowRightIcon, Link2Icon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useConversations } from "@/hooks/useConversations";
import { useChangeSessionLink, useSessionLinks } from "@/hooks/useSessionLinks";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import type { SessionLink } from "@/lib/sessionLinksApi";

export function SessionLinkDetails({ link }: { link: SessionLink }) {
  const change = useChangeSessionLink();
  return (
    <div className="space-y-3 rounded-lg border p-3" data-testid="session-link">
      <div className="flex items-center gap-2 text-sm font-medium">
        <span className="min-w-0 flex-1 break-words">{link.source_title || link.source_id}</span>
        <ArrowRightIcon aria-hidden className="size-4 shrink-0 text-brand-accent" />
        <span className="min-w-0 flex-1 break-words">{link.target_title || link.target_id}</span>
      </div>
      <p className="text-xs text-muted-foreground">
        {link.active ? "Connected" : "Inactive · needs fresh approval"} · Approved by{" "}
        {link.approved_by}
      </p>
      {link.can_disconnect && (
        <Button
          variant="outline"
          size="sm"
          disabled={change.isPending}
          onClick={() =>
            change.mutate({
              sourceId: link.source_id,
              targetId: link.target_id,
              disconnect: true,
            })
          }
        >
          {change.isPending ? "Disconnecting…" : "Disconnect"}
        </Button>
      )}
      {change.error && (
        <p role="alert" className="text-sm text-destructive">
          {change.error.message}
        </p>
      )}
    </div>
  );
}

function LinkList({
  sessionId,
  incoming,
  open,
}: {
  sessionId: string;
  incoming: boolean;
  open: boolean;
}) {
  const links = useSessionLinks(sessionId, incoming, open);
  const rows = links.data?.pages.flatMap((page) => page.data) ?? [];
  return (
    <section className="space-y-2" aria-label={incoming ? "Incoming links" : "Outgoing links"}>
      <h3 className="text-sm font-medium">{incoming ? "Requests from" : "Send requests to"}</h3>
      {links.isLoading && <p className="text-sm text-muted-foreground">Loading links…</p>}
      {links.error && (
        <p role="alert" className="text-sm text-destructive">
          {links.error.message}
        </p>
      )}
      {links.isSuccess && rows.length === 0 && (
        <p className="text-sm text-muted-foreground">No links.</p>
      )}
      {rows.map((link) => (
        <SessionLinkDetails key={`${link.source_id}:${link.target_id}`} link={link} />
      ))}
      {links.hasNextPage && (
        <Button
          variant="ghost"
          size="sm"
          disabled={links.isFetchingNextPage}
          onClick={() => void links.fetchNextPage()}
        >
          Load more links
        </Button>
      )}
    </section>
  );
}

export function SessionLinksDialog({
  sessionId,
  title,
  canConnect,
  open,
  onOpenChange,
}: {
  sessionId: string;
  title?: string | null;
  canConnect: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [search, setSearch] = useState("");
  const [target, setTarget] = useState<{ id: string; title: string } | null>(null);
  const change = useChangeSessionLink();
  const candidates = useConversations(
    search,
    false,
    { enabled: open && canConnect },
    undefined,
    "mine",
  );
  const rows =
    candidates.data?.pages
      .flatMap((page) => page.data)
      .filter((row) => row.id !== sessionId && row.permission_level === 4 && !row.archived) ?? [];
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Session links</DialogTitle>
          <DialogDescription>
            Connect {title || "this session"} to existing sessions you own. Each arrow permits
            requests in one direction.
          </DialogDescription>
        </DialogHeader>
        <p className="rounded-md bg-muted px-3 py-2 text-sm text-muted-foreground">
          Replies sent back here are visible to everyone who can read this session, now or later.
          Each person requesting work must also have their own edit access to the other session.
        </p>
        <LinkList sessionId={sessionId} incoming={false} open={open} />
        <LinkList sessionId={sessionId} incoming open={open} />
        {canConnect ? (
          <section className="space-y-3 border-t pt-4" aria-label="Connect a session">
            <h3 className="text-sm font-medium">Connect a session</h3>
            <Input
              aria-label="Find a session you own"
              placeholder="Find a session you own…"
              value={search}
              onChange={(event) => {
                setSearch(event.target.value);
                setTarget(null);
              }}
            />
            <div
              className="max-h-44 overflow-y-auto rounded-md border"
              role="group"
              aria-label="Available sessions"
            >
              {candidates.isLoading && (
                <p className="p-3 text-sm text-muted-foreground">Loading sessions…</p>
              )}
              {rows.map((row) => (
                <button
                  key={row.id}
                  type="button"
                  aria-pressed={target?.id === row.id}
                  className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-sm hover:bg-muted aria-pressed:bg-brand-accent/10"
                  onClick={() => setTarget({ id: row.id, title: row.title || "Untitled session" })}
                >
                  <span className="truncate">{row.title || "Untitled session"}</span>
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {row.id.slice(0, 8)}
                  </span>
                </button>
              ))}
              {candidates.isSuccess && rows.length === 0 && (
                <p className="p-3 text-sm text-muted-foreground">No matching sessions.</p>
              )}
            </div>
            {candidates.hasNextPage && (
              <Button
                variant="ghost"
                size="sm"
                disabled={candidates.isFetchingNextPage}
                onClick={() => void candidates.fetchNextPage()}
              >
                Load more sessions
              </Button>
            )}
            {candidates.error && (
              <p role="alert" className="text-sm text-destructive">
                {candidates.error.message}
              </p>
            )}
            {target && (
              <p className="text-sm">
                {title || "This session"} → {target.title}
              </p>
            )}
            <Button
              disabled={!target || change.isPending}
              onClick={() =>
                target &&
                change.mutate(
                  {
                    sourceId: sessionId,
                    targetId: target.id,
                  },
                  { onSuccess: () => setTarget(null) },
                )
              }
            >
              {change.isPending ? "Connecting…" : "Connect"}
            </Button>
            {change.error && (
              <p role="alert" className="text-sm text-destructive">
                {change.error.message}
              </p>
            )}
          </section>
        ) : (
          <p className="text-sm text-muted-foreground">
            Creating links requires ownership of both sessions.
          </p>
        )}
        <p className="text-xs text-muted-foreground">
          Either current session owner can disconnect. Disconnecting stops new linked work and
          replies. Accepted work continues; replies already copied here remain.
        </p>
      </DialogContent>
    </Dialog>
  );
}

export function SessionLinksButton({
  sessionId,
  title,
  permissionLevel,
}: {
  sessionId: string;
  title?: string | null;
  permissionLevel?: number | null;
}) {
  const enabled = isFeatureEnabled(useServerInfo(), "session_links");
  const [open, setOpen] = useState(false);
  if (!enabled) return null;
  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        aria-label="Session links"
        title="Session links"
        onClick={() => setOpen(true)}
      >
        <Link2Icon className="size-4" />
      </Button>
      {open && (
        <SessionLinksDialog
          key={sessionId}
          sessionId={sessionId}
          title={title}
          canConnect={permissionLevel === 4}
          open={open}
          onOpenChange={setOpen}
        />
      )}
    </>
  );
}
