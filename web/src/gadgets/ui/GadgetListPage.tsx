/**
 * Gadget page (`/gadget`): the viewer's gadgets and those shared with them, laid out like the
 * Automations page. New opens the New gadget dialog; a row opens its gadget full screen.
 */
import { toast } from "sonner";
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
import { gadgetSharingAvailable } from "@/lib/permissionsApi";
import { useNavigate } from "@/lib/routing";
import { cn } from "@/lib/utils";
import type { SharedGadget, Gadget } from "@/gadgets/api/gadgetApi";
import { gadgetBoardPath } from "@/gadgets/host/gadgetPages";
import { DeleteGadgetDialog } from "./DeleteGadgetDialog";
import { GadgetErrorNotice } from "./GadgetErrorNotice";
import { GadgetStartDialog } from "./GadgetStart";
import { MAX_GADGET_NAME_LENGTH } from "./GadgetTemplateGallery";
import { IN_APP_GADGET_OPEN, gadgetErrorMessage } from "./gadgetNavigation";
import {
  usePinGadget,
  useRenameGadget,
  useSharedGadgets,
  useGadgets,
  useGadgetShelf,
} from "./gadgetQueries";

function sharedSubtitle(gadget: SharedGadget): string {
  const owner = gadget.owner_email ?? "someone";
  return gadget.shared_via === "group"
    ? `Shared by ${owner} with a group you’re in`
    : `Shared by ${owner}`;
}

/** The viewer's own gadget's second line, so every card has one. */
function ownSubtitle(gadget: Gadget, pinned: boolean): string {
  const state = gadget.published_revision ? "Published" : "Not published yet";
  return pinned ? `${state} · Pinned to sidebar` : state;
}

/** One gadget as a card like an automation's row; it opens the gadget full screen. */
function GadgetRow({
  gadget,
  subtitle,
  pinned,
  onDelete,
  onRename,
}: {
  gadget: Gadget;
  subtitle: string;
  /** Whether it is pinned to the sidebar; absent where the row offers no Pin. */
  pinned?: boolean;
  /** Absent for a viewer who can't delete it. */
  onDelete?: () => void;
  /** Absent for a viewer who can't rename it. */
  onRename?: () => void;
}) {
  const navigate = useNavigate();
  const pin = usePinGadget();
  const hasMenu = onDelete !== undefined || onRename !== undefined || pinned !== undefined;
  return (
    <div
      data-testid="gadget-row"
      data-gadget-id={gadget.id}
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
        aria-label={`Open ${gadget.name}`}
        onClick={() => navigate(gadgetBoardPath(gadget.id), { state: IN_APP_GADGET_OPEN })}
        className="flex min-w-0 flex-1 cursor-pointer flex-col gap-0.5 text-left outline-none after:absolute after:inset-0 after:content-['']"
      >
        <span className="truncate text-[15px] font-semibold">{gadget.name}</span>
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
              aria-label={`Actions for ${gadget.name}`}
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
                onSelect={() =>
                  pin.mutate(
                    { gadgetId: gadget.id, pinned: !pinned },
                    {
                      onError: () =>
                        toast.error(
                          pinned ? "Couldn’t unpin the gadget" : "Couldn’t pin the gadget",
                        ),
                    },
                  )
                }
              >
                {pinned ? <PinOffIcon className="size-4" /> : <PinIcon className="size-4" />}
                {pinned ? "Unpin from sidebar" : "Pin to sidebar"}
              </DropdownMenuItem>
            )}
            {onDelete && (
              <DropdownMenuItem variant="destructive" onSelect={onDelete}>
                <Trash2Icon className="size-4" />
                Delete gadget
              </DropdownMenuItem>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  );
}

/** Renames a gadget from its row, as its name does in place inside the gadget. */
function RenameGadgetDialog({
  gadget,
  onClose,
  onCloseAutoFocus,
}: {
  gadget: Pick<Gadget, "id" | "name">;
  onClose: () => void;
  onCloseAutoFocus: (event: Event) => void;
}) {
  const rename = useRenameGadget();
  const [name, setName] = useState(gadget.name);
  const fieldId = useId();
  const trimmed = name.trim();
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!trimmed || rename.isPending) return;
    if (trimmed === gadget.name) {
      onClose();
      return;
    }
    rename.mutate({ gadgetId: gadget.id, name: trimmed }, { onSuccess: onClose });
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
            <DialogTitle>Rename gadget</DialogTitle>
          </DialogHeader>
          <div className="flex flex-col gap-1.5">
            <label htmlFor={fieldId} className="text-sm font-medium text-muted-foreground">
              Name
            </label>
            <Input
              id={fieldId}
              value={name}
              autoFocus
              maxLength={MAX_GADGET_NAME_LENGTH}
              onFocus={(event) => event.currentTarget.select()}
              onChange={(event) => setName(event.target.value)}
            />
          </div>
          {rename.isError && (
            <GadgetErrorNotice
              title="Couldn’t rename the gadget"
              error={gadgetErrorMessage(rename.error)}
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
              componentId="gadgets.rename"
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

/** Loaded apart from the viewer's own gadgets, so a slow lookup doesn't hold those up. */
function SharedGadgets({ shared }: { shared: ReturnType<typeof useSharedGadgets> }) {
  let content;
  if (shared.isPending) {
    content = <Loading label="Looking for gadgets shared with you…" />;
  } else if (shared.isError) {
    content = (
      <GadgetErrorNotice
        title="Couldn’t load the gadgets shared with you"
        error={shared.error}
        onRetry={() => void shared.refetch()}
      />
    );
  } else if (shared.data.length === 0) {
    content = (
      <p className="text-ui text-muted-foreground">
        Nothing yet. Gadgets people share with you or your groups show up here.
      </p>
    );
  } else {
    content = (
      <div className="flex flex-col gap-2" data-testid="shared-gadgets-list">
        {shared.data.map((gadget) => (
          <GadgetRow key={gadget.id} gadget={gadget} subtitle={sharedSubtitle(gadget)} />
        ))}
      </div>
    );
  }
  return (
    <section className="mt-8" aria-labelledby="shared-gadgets-heading">
      <h2 id="shared-gadgets-heading" className="mb-2 text-sm font-medium text-muted-foreground">
        Shared with you
      </h2>
      {content}
    </section>
  );
}

function EmptyState({ onCreate }: { onCreate: () => void }) {
  return (
    <div className="flex flex-col items-center gap-2 py-16 text-center" data-testid="gadgets-empty">
      <PanelsTopLeftIcon className="size-8 text-muted-foreground/50" />
      <p className="text-ui font-medium">No gadgets yet</p>
      <p className="max-w-sm text-sm text-muted-foreground">
        A gadget is a small app an agent builds for you: a board, a tracker, a report. Describe one
        and a session builds it, or start from a template.
      </p>
      <Button className="mt-2" componentId="gadgets.empty.create" onClick={onCreate}>
        Create gadget
      </Button>
    </div>
  );
}

export function GadgetListPage() {
  const gadgets = useGadgets();
  const info = useServerInfo();
  const sharingAvailable = gadgetSharingAvailable(info);
  const shared = useSharedGadgets(sharingAvailable);
  const shelf = useGadgetShelf();
  const [creating, setCreating] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Gadget | null>(null);
  const [renameTarget, setRenameTarget] = useState<Gadget | null>(null);
  const newButtonRef = useRef<HTMLButtonElement>(null);
  // The server lists them oldest first; the newest, usually the one just made, goes on top.
  const newestFirst = useMemo(() => gadgets.data?.slice().reverse(), [gadgets.data]);
  const pinnedIds = useMemo(
    () => new Set(shelf.data?.pinned.map((pin) => pin.id) ?? []),
    [shelf.data],
  );
  // A row's dialog hands focus back to the row's ⋯ button; after a delete, to the row that took
  // its place (below it, else above it), else to New.
  const rowOrder = useRef<string[]>([]);
  rowOrder.current = [...(newestFirst ?? []), ...(shared.data ?? [])].map((gadget) => gadget.id);
  const focusRow = (event: Event, gadgetId: string) => {
    event.preventDefault();
    const row = (id: string) =>
      document.querySelector<HTMLElement>(
        `[data-testid=gadget-row][data-gadget-id="${CSS.escape(id)}"]`,
      );
    const own = row(gadgetId);
    if (own) {
      own.querySelector<HTMLElement>("[aria-haspopup=menu]")?.focus();
      return;
    }
    const order = rowOrder.current;
    const index = order.indexOf(gadgetId);
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
        <h1 className="text-2xl font-medium">Gadget</h1>
        <Button
          ref={newButtonRef}
          data-testid="new-gadget-button"
          className="shrink-0"
          componentId="gadgets.new"
          onClick={() => setCreating(true)}
        >
          <PlusIcon />
          New
        </Button>
      </div>

      <section aria-label="Your gadgets">
        {gadgets.isError ? (
          <GadgetErrorNotice
            title="Couldn’t load your gadgets"
            error={gadgets.error}
            onRetry={() => void gadgets.refetch()}
          />
        ) : newestFirst === undefined ? (
          <Loading label="Loading gadgets…" />
        ) : newestFirst.length === 0 ? (
          <EmptyState onCreate={() => setCreating(true)} />
        ) : (
          <div className="flex flex-col gap-2" data-testid="gadgets-list">
            {newestFirst.map((gadget) => (
              <GadgetRow
                key={gadget.id}
                gadget={gadget}
                subtitle={ownSubtitle(gadget, pinnedIds.has(gadget.id))}
                pinned={shelf.data ? pinnedIds.has(gadget.id) : undefined}
                onRename={gadget.permissions.publish ? () => setRenameTarget(gadget) : undefined}
                onDelete={gadget.permissions.manage ? () => setDeleteTarget(gadget) : undefined}
              />
            ))}
          </div>
        )}
      </section>
      {/* Nothing can be shared with one user, as Automations leaves out its Shared tab. */}
      {sharingAvailable && <SharedGadgets shared={shared} />}

      {deleteTarget && (
        <DeleteGadgetDialog
          gadget={deleteTarget}
          onClose={() => setDeleteTarget(null)}
          onCloseAutoFocus={(event) => focusRow(event, deleteTarget.id)}
        />
      )}
      {renameTarget && (
        <RenameGadgetDialog
          gadget={renameTarget}
          onClose={() => setRenameTarget(null)}
          onCloseAutoFocus={(event) => focusRow(event, renameTarget.id)}
        />
      )}
      <GadgetStartDialog open={creating} onOpenChange={setCreating} returnFocusRef={newButtonRef} />
    </PageScroll>
  );
}
