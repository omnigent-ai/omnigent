/**
 * Thingy page (`/thingy`): the viewer's thingies and those shared with them, laid out like the
 * Automations page. New opens the New thingy dialog; a row opens its thingy full screen.
 */
import { useState } from "react";
import { Loader2Icon, MoreHorizontalIcon, PanelsTopLeftIcon, PlusIcon } from "lucide-react";

import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useNavigate } from "@/lib/routing";
import { cn } from "@/lib/utils";
import type { SharedThingy, Thingy } from "@/thingies/api/thingyApi";
import { thingyBoardPath } from "@/thingies/host/thingyPages";
import { DeleteThingyDialog } from "./DeleteThingyDialog";
import { ThingyErrorNotice } from "./ThingyErrorNotice";
import { ThingyStartDialog } from "./ThingyStart";
import { useSharedThingies, useThingies } from "./thingyQueries";

function sharedSubtitle(thingy: SharedThingy): string {
  const owner = thingy.owner_email ?? "someone";
  return thingy.shared_via === "group"
    ? `Shared by ${owner} with a group you’re in`
    : `Shared by ${owner}`;
}

/** One thingy as a card like an automation's row; it opens the thingy full screen. */
function ThingyRow({
  thingy,
  subtitle,
  onDelete,
}: {
  thingy: Thingy;
  subtitle?: string;
  /** Absent for a viewer who can't delete it. */
  onDelete?: () => void;
}) {
  const navigate = useNavigate();
  return (
    <div
      data-testid="thingy-row"
      className={cn(
        "group relative flex items-center gap-3 rounded-xl border border-border bg-card py-3 pl-4 transition-colors hover:bg-muted/40",
        onDelete ? "pr-12" : "pr-4",
      )}
    >
      {/* The body's hit area stretches across the card, under the ⋯ trigger. */}
      <button
        type="button"
        aria-label={`Open ${thingy.name}`}
        onClick={() => navigate(thingyBoardPath(thingy.id))}
        className="flex min-w-0 flex-1 cursor-pointer flex-col gap-0.5 text-left after:absolute after:inset-0 after:content-['']"
      >
        <span className="truncate text-[15px] font-semibold">{thingy.name}</span>
        {subtitle !== undefined ? (
          <span className="truncate text-ui text-muted-foreground/80">{subtitle}</span>
        ) : (
          !thingy.published_revision && (
            <span className="text-xs font-medium text-muted-foreground">Not published yet</span>
          )
        )}
      </button>
      {onDelete && (
        // Hidden until the row is hovered or focused on desktop, as on automation rows.
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label={`Actions for ${thingy.name}`}
              className={cn(
                "-translate-y-1/2 absolute top-1/2 right-3 z-10 transition-opacity",
                "md:opacity-0 md:group-hover:opacity-100 md:group-has-[:focus-visible]:opacity-100",
                "md:aria-expanded:opacity-100",
              )}
            >
              <MoreHorizontalIcon className="size-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-40">
            <DropdownMenuItem variant="destructive" onSelect={onDelete}>
              Delete thingy
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  );
}

function Loading({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2 py-12 text-ui text-muted-foreground">
      <Loader2Icon className="size-4 animate-spin" />
      {label}
    </div>
  );
}

/** Loaded apart from the viewer's own thingies, so a slow lookup doesn't hold those up. */
function SharedThingies() {
  const shared = useSharedThingies();
  let content;
  if (shared.isPending) {
    content = <Loading label="Looking for thingies shared with you…" />;
  } else if (shared.isError) {
    content = (
      <ThingyErrorNotice
        title="Couldn’t load the thingies shared with you"
        error={shared.error}
        onRetry={() => void shared.refetch()}
      />
    );
  } else if (shared.data.length === 0) {
    content = (
      <p className="text-ui text-muted-foreground">
        Nothing yet. Thingies people share with you or your groups show up here.
      </p>
    );
  } else {
    content = (
      <div className="flex flex-col gap-2" data-testid="shared-thingies-list">
        {shared.data.map((thingy) => (
          <ThingyRow key={thingy.id} thingy={thingy} subtitle={sharedSubtitle(thingy)} />
        ))}
      </div>
    );
  }
  return (
    <section className="mt-8" aria-labelledby="shared-thingies-heading">
      <h2 id="shared-thingies-heading" className="mb-2 text-sm font-medium text-muted-foreground">
        Shared with you
      </h2>
      {content}
    </section>
  );
}

function EmptyState({ onCreate }: { onCreate: () => void }) {
  return (
    <div
      className="flex flex-col items-center gap-2 py-16 text-center"
      data-testid="thingies-empty"
    >
      <PanelsTopLeftIcon className="size-8 text-muted-foreground/50" />
      <p className="text-ui font-medium">No thingies yet</p>
      <p className="max-w-sm text-sm text-muted-foreground">
        A thingy is a small app an agent builds for you: a board, a tracker, a report. Describe one
        and a session builds it, or start from a template.
      </p>
      <Button className="mt-2" componentId="thingies.empty.create" onClick={onCreate}>
        Create thingy
      </Button>
    </div>
  );
}

export function ThingyListPage() {
  const thingies = useThingies();
  const [creating, setCreating] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Thingy | null>(null);

  return (
    <PageScroll
      maxWidthClassName="max-w-4xl"
      className="[scrollbar-gutter:stable]"
      contentClassName="px-4 md:px-8"
    >
      <div className="mb-6 flex items-start justify-between gap-4">
        <h1 className="text-2xl font-medium">Thingy</h1>
        <Button
          data-testid="new-thingy-button"
          className="shrink-0"
          componentId="thingies.new"
          onClick={() => setCreating(true)}
        >
          <PlusIcon />
          New
        </Button>
      </div>

      <section aria-label="Your thingies">
        {thingies.isError ? (
          <ThingyErrorNotice
            title="Couldn’t load your thingies"
            error={thingies.error}
            onRetry={() => void thingies.refetch()}
          />
        ) : thingies.isPending ? (
          <Loading label="Loading thingies…" />
        ) : thingies.data.length === 0 ? (
          <EmptyState onCreate={() => setCreating(true)} />
        ) : (
          <div className="flex flex-col gap-2" data-testid="thingies-list">
            {thingies.data.map((thingy) => (
              <ThingyRow
                key={thingy.id}
                thingy={thingy}
                onDelete={thingy.permissions.manage ? () => setDeleteTarget(thingy) : undefined}
              />
            ))}
          </div>
        )}
      </section>
      <SharedThingies />

      {deleteTarget && (
        <DeleteThingyDialog thingy={deleteTarget} onClose={() => setDeleteTarget(null)} />
      )}
      <ThingyStartDialog open={creating} onOpenChange={setCreating} />
    </PageScroll>
  );
}
