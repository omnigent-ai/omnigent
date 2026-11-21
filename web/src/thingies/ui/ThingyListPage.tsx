/**
 * Thingy page (`/thingy`): the viewer's thingies and those shared with them, laid out like the
 * Automations page. New opens the New thingy dialog; a row opens its thingy full screen.
 */
import { type FormEvent, useId, useMemo, useRef, useState } from "react";
import {
  Loader2Icon,
  MoreHorizontalIcon,
  PanelsTopLeftIcon,
  PencilIcon,
  PinIcon,
  PinOffIcon,
  PlusIcon,
  Trash2Icon,
} from "lucide-react";

import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { thingySharingAvailable } from "@/lib/permissionsApi";
import { useNavigate } from "@/lib/routing";
import { cn } from "@/lib/utils";
import type { SharedThingy, Thingy } from "@/thingies/api/thingyApi";
import { thingyBoardPath } from "@/thingies/host/thingyPages";
import { DeleteThingyDialog } from "./DeleteThingyDialog";
import { ThingyErrorNotice } from "./ThingyErrorNotice";
import { ThingyStartDialog } from "./ThingyStart";
import { MAX_THINGY_NAME_LENGTH } from "./ThingyTemplateGallery";
import { IN_APP_THINGY_OPEN, thingyErrorMessage } from "./thingyNavigation";
import {
  usePinThingy,
  useRenameThingy,
  useSharedThingies,
  useThingies,
  useThingyShelf,
} from "./thingyQueries";

function sharedSubtitle(thingy: SharedThingy): string {
  const owner = thingy.owner_email ?? "someone";
  return thingy.shared_via === "group"
    ? `Shared by ${owner} with a group you’re in`
    : `Shared by ${owner}`;
}

/** The viewer's own thingy's second line, so every card has one. */
function ownSubtitle(thingy: Thingy, pinned: boolean): string {
  const state = thingy.published_revision ? "Published" : "Not published yet";
  return pinned ? `${state} · Pinned to sidebar` : state;
}

/** One thingy as a card like an automation's row; it opens the thingy full screen. */
function ThingyRow({
  thingy,
  subtitle,
  pinned,
  onDelete,
  onRename,
}: {
  thingy: Thingy;
  subtitle: string;
  /** Whether it is pinned to the sidebar; absent where the row offers no Pin. */
  pinned?: boolean;
  /** Absent for a viewer who can't delete it. */
  onDelete?: () => void;
  /** Absent for a viewer who can't rename it. */
  onRename?: () => void;
}) {
  const navigate = useNavigate();
  const pin = usePinThingy();
  const hasMenu = onDelete !== undefined || onRename !== undefined || pinned !== undefined;
  return (
    <div
      data-testid="thingy-row"
      data-thingy-id={thingy.id}
      className={cn(
        "group relative flex items-center gap-3 rounded-xl border border-border bg-card py-3 pl-4 transition-colors hover:bg-muted/40",
        // The card shows the keyboard focus of the button that covers it.
        "has-[[data-row-open]:focus-visible]:ring-3 has-[[data-row-open]:focus-visible]:ring-ring/50",
        hasMenu ? "pr-12" : "pr-4",
      )}
    >
      {/* The body's hit area stretches across the card, under the ⋯ trigger. */}
      <button
        type="button"
        data-row-open
        aria-label={`Open ${thingy.name}`}
        onClick={() => navigate(thingyBoardPath(thingy.id), { state: IN_APP_THINGY_OPEN })}
        className="flex min-w-0 flex-1 cursor-pointer flex-col gap-0.5 text-left outline-none after:absolute after:inset-0 after:content-['']"
      >
        <span className="truncate text-[15px] font-semibold">{thingy.name}</span>
        <span className="truncate text-ui text-muted-foreground/80">{subtitle}</span>
      </button>
      {hasMenu && (
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
            {onRename && (
              <DropdownMenuItem onSelect={onRename}>
                <PencilIcon className="size-4" />
                Rename
              </DropdownMenuItem>
            )}
            {pinned !== undefined && (
              <DropdownMenuItem
                onSelect={() => pin.mutate({ thingyId: thingy.id, pinned: !pinned })}
              >
                {pinned ? <PinOffIcon className="size-4" /> : <PinIcon className="size-4" />}
                {pinned ? "Unpin from sidebar" : "Pin to sidebar"}
              </DropdownMenuItem>
            )}
            {onDelete && (
              <DropdownMenuItem variant="destructive" onSelect={onDelete}>
                <Trash2Icon className="size-4" />
                Delete thingy
              </DropdownMenuItem>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  );
}

/** Renames a thingy from its row, as its name does in place inside the thingy. */
function RenameThingyDialog({
  thingy,
  onClose,
  onCloseAutoFocus,
}: {
  thingy: Pick<Thingy, "id" | "name">;
  onClose: () => void;
  onCloseAutoFocus: (event: Event) => void;
}) {
  const rename = useRenameThingy();
  const [name, setName] = useState(thingy.name);
  const fieldId = useId();
  const trimmed = name.trim();
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!trimmed || rename.isPending) return;
    if (trimmed === thingy.name) {
      onClose();
      return;
    }
    rename.mutate({ thingyId: thingy.id, name: trimmed }, { onSuccess: onClose });
  };
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !rename.isPending) onClose();
      }}
    >
      <DialogContent className="sm:max-w-md" onCloseAutoFocus={onCloseAutoFocus}>
        <form onSubmit={submit} className="flex flex-col gap-4">
          <DialogHeader>
            <DialogTitle>Rename thingy</DialogTitle>
          </DialogHeader>
          <div className="flex flex-col gap-1.5">
            <label htmlFor={fieldId} className="text-sm font-medium text-muted-foreground">
              Name
            </label>
            <Input
              id={fieldId}
              value={name}
              autoFocus
              maxLength={MAX_THINGY_NAME_LENGTH}
              onFocus={(event) => event.currentTarget.select()}
              onChange={(event) => setName(event.target.value)}
            />
          </div>
          {rename.isError && (
            <ThingyErrorNotice
              title="Couldn’t rename the thingy"
              error={thingyErrorMessage(rename.error)}
            />
          )}
          <DialogFooter className="border-t-0 bg-transparent">
            <Button type="button" variant="outline" disabled={rename.isPending} onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="submit"
              loading={rename.isPending}
              disabled={!trimmed}
              componentId="thingies.rename"
            >
              Rename
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
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
function SharedThingies({ shared }: { shared: ReturnType<typeof useSharedThingies> }) {
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
  const info = useServerInfo();
  const sharingAvailable = thingySharingAvailable(info);
  const shared = useSharedThingies(sharingAvailable);
  const shelf = useThingyShelf();
  const [creating, setCreating] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Thingy | null>(null);
  const [renameTarget, setRenameTarget] = useState<Thingy | null>(null);
  const newButtonRef = useRef<HTMLButtonElement>(null);
  // The server lists them oldest first; the newest, usually the one just made, goes on top.
  const newestFirst = useMemo(() => thingies.data?.slice().reverse(), [thingies.data]);
  const pinnedIds = useMemo(
    () => new Set(shelf.data?.pinned.map((pin) => pin.id) ?? []),
    [shelf.data],
  );
  // A row's dialog hands focus back to the row's ⋯ button; after a delete, to the row that took
  // its place (below it, else above it), else to New.
  const rowOrder = useRef<string[]>([]);
  rowOrder.current = [...(newestFirst ?? []), ...(shared.data ?? [])].map((thingy) => thingy.id);
  const focusRow = (event: Event, thingyId: string) => {
    event.preventDefault();
    const row = (id: string) =>
      document.querySelector<HTMLElement>(
        `[data-testid=thingy-row][data-thingy-id="${CSS.escape(id)}"]`,
      );
    const own = row(thingyId);
    if (own) {
      own.querySelector<HTMLElement>("[aria-haspopup=menu]")?.focus();
      return;
    }
    const order = rowOrder.current;
    const index = order.indexOf(thingyId);
    const next = [...order.slice(index + 1), ...order.slice(0, index).reverse()]
      .map(row)
      .find((element) => element !== null);
    (next?.querySelector<HTMLElement>("[data-row-open]") ?? newButtonRef.current)?.focus();
  };

  return (
    <PageScroll
      maxWidthClassName="max-w-4xl"
      className="[scrollbar-gutter:stable]"
      contentClassName="px-4 md:px-8"
    >
      <div className="mb-6 flex items-start justify-between gap-4">
        <h1 className="text-2xl font-medium">Thingy</h1>
        <Button
          ref={newButtonRef}
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
        ) : newestFirst === undefined ? (
          <Loading label="Loading thingies…" />
        ) : newestFirst.length === 0 ? (
          <EmptyState onCreate={() => setCreating(true)} />
        ) : (
          <div className="flex flex-col gap-2" data-testid="thingies-list">
            {newestFirst.map((thingy) => (
              <ThingyRow
                key={thingy.id}
                thingy={thingy}
                subtitle={ownSubtitle(thingy, pinnedIds.has(thingy.id))}
                pinned={shelf.data ? pinnedIds.has(thingy.id) : undefined}
                onRename={thingy.permissions.publish ? () => setRenameTarget(thingy) : undefined}
                onDelete={thingy.permissions.manage ? () => setDeleteTarget(thingy) : undefined}
              />
            ))}
          </div>
        )}
      </section>
      {/* Nothing can be shared with one user, as Automations leaves out its Shared tab. */}
      {sharingAvailable && <SharedThingies shared={shared} />}

      {deleteTarget && (
        <DeleteThingyDialog
          thingy={deleteTarget}
          onClose={() => setDeleteTarget(null)}
          onCloseAutoFocus={(event) => focusRow(event, deleteTarget.id)}
        />
      )}
      {renameTarget && (
        <RenameThingyDialog
          thingy={renameTarget}
          onClose={() => setRenameTarget(null)}
          onCloseAutoFocus={(event) => focusRow(event, renameTarget.id)}
        />
      )}
      <ThingyStartDialog open={creating} onOpenChange={setCreating} returnFocusRef={newButtonRef} />
    </PageScroll>
  );
}
