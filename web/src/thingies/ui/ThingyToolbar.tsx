// A thingy's controls in its side panel tab: its inline-editable name, Pin, Comments, Share and a
// ⋯ menu with Thingy details and Delete. In full screen they move into the panel's top bar.
import { type KeyboardEvent, useState } from "react";
import {
  EllipsisVerticalIcon,
  MessageSquareIcon,
  PinIcon,
  PinOffIcon,
  UserPlusIcon,
} from "lucide-react";

import { PermissionsModal } from "@/components/PermissionsModal";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { thingySharingAvailable } from "@/lib/permissionsApi";
import { cn } from "@/lib/utils";
import type { Thingy, ThingyContent } from "@/thingies/api/thingyApi";
import { DeleteThingyDialog } from "./DeleteThingyDialog";
import { MAX_THINGY_NAME_LENGTH } from "./ThingyTemplateGallery";
import { thingyErrorMessage } from "./thingyNavigation";
import { usePinThingy, useRenameThingy, useThingyShelf } from "./thingyQueries";
import type { ThingyComments } from "./useThingyComments";

/**
 * The thingy's name. A viewer who can edit it clicks the name to rename it in place, as a
 * session's title in its header: Enter or leaving the field saves, Escape cancels.
 */
export function ThingyName({ thingy }: { thingy: Thingy }) {
  const rename = useRenameThingy();
  const [draft, setDraft] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (!thingy.permissions.publish) {
    return <span className="min-w-0 truncate text-ui font-medium">{thingy.name}</span>;
  }
  const save = () => {
    if (draft === null || rename.isPending) return;
    const name = draft.trim();
    if (!name || name === thingy.name) {
      setDraft(null);
      setError(null);
      return;
    }
    if (name.length > MAX_THINGY_NAME_LENGTH) {
      setError(`Use at most ${MAX_THINGY_NAME_LENGTH} characters.`);
      return;
    }
    setError(null);
    rename.mutate(
      { thingyId: thingy.id, name },
      {
        onSuccess: () => setDraft(null),
        onError: (renameError) => setError(`Couldn’t rename: ${thingyErrorMessage(renameError)}`),
      },
    );
  };
  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      event.preventDefault();
      save();
    } else if (event.key === "Escape") {
      event.preventDefault();
      setDraft(null);
      setError(null);
    }
  };
  return (
    <span className="flex min-w-0 items-center gap-2">
      {draft === null ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              onClick={() => {
                setError(null);
                setDraft(thingy.name);
              }}
              className="-mx-1 min-w-0 cursor-text truncate rounded-md border border-transparent px-1 text-left text-ui font-medium hover:border-border"
            >
              {thingy.name}
            </button>
          </TooltipTrigger>
          <TooltipContent side="bottom">Rename</TooltipContent>
        </Tooltip>
      ) : (
        <input
          aria-label="Thingy name"
          aria-invalid={error !== null || undefined}
          // The field replaces the name the viewer just clicked.
          autoFocus
          value={draft}
          disabled={rename.isPending}
          onFocus={(event) => event.currentTarget.select()}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKeyDown}
          onBlur={save}
          className="-mx-1 h-6 min-w-0 rounded-md border border-ring bg-background px-1 text-ui font-medium outline-none"
          style={{ width: `${Math.max(draft.length, 8) + 2}ch`, maxWidth: "100%" }}
        />
      )}
      {error !== null && (
        <span role="alert" className="shrink-0 truncate text-sm text-destructive">
          {error}
        </span>
      )}
    </span>
  );
}

const TOOLBAR_ICON_BUTTON = "size-6 shrink-0 text-muted-foreground hover:text-foreground";

/** Pins the thingy for the viewer, so the sidebar lists it under Thingy. */
export function ThingyPinButton({ thingy }: { thingy: Pick<Thingy, "id"> }) {
  const shelf = useThingyShelf();
  const pin = usePinThingy();
  const pinned = shelf.data?.pinned.some((item) => item.id === thingy.id) ?? false;
  const label = pinned ? "Unpin from sidebar" : "Pin to sidebar";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label={label}
          aria-pressed={pinned}
          disabled={shelf.data === undefined || pin.isPending}
          className={TOOLBAR_ICON_BUTTON}
          componentId="thingies.pin"
          onClick={() => pin.mutate({ thingyId: thingy.id, pinned: !pinned })}
        >
          {pinned ? <PinOffIcon className="size-4" /> : <PinIcon className="size-4" />}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom">
        {pin.isError ? `Couldn’t change the pin: ${thingyErrorMessage(pin.error)}` : label}
      </TooltipContent>
    </Tooltip>
  );
}

/** Shows or hides the comments beside the thingy, with the number of open ones. */
export function ThingyCommentsButton({ comments }: { comments: ThingyComments }) {
  const count = comments.draftCount;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="xs"
          aria-pressed={comments.open}
          aria-label={count > 0 ? `Comments (${count} open)` : "Comments"}
          className={cn(
            "shrink-0 text-muted-foreground hover:text-foreground",
            comments.open && "bg-muted text-foreground",
          )}
          componentId="thingies.comments"
          onClick={() => comments.setOpen(!comments.open)}
        >
          <MessageSquareIcon className="size-3.5" />
          Comments
          {count > 0 && (
            <span className="rounded-full bg-muted px-1.5 text-[10px] tabular-nums">{count}</span>
          )}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom">
        {comments.canComment
          ? "Select something in the thingy and choose Add comment"
          : comments.open
            ? "Hide comments"
            : "Show comments"}
      </TooltipContent>
    </Tooltip>
  );
}

const CAPABILITY_LABELS: Record<string, string> = {
  "data.read": "Read its data",
  "data.write": "Change its data",
  "sessions.read": "Read your sessions",
  "sessions.create": "Start sessions (asks you first)",
};

function ThingyDetailsDialog({
  thingy,
  content,
  open,
  onOpenChange,
}: {
  thingy: Thingy;
  content: ThingyContent | undefined;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Thingy details</DialogTitle>
          <DialogDescription className="break-all">{thingy.name}</DialogDescription>
        </DialogHeader>
        <dl className="m-0 grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-3 text-ui">
          <dt className="text-muted-foreground">Published version</dt>
          <dd className="m-0 min-w-0">
            {thingy.published_revision ? (
              <code className="break-all font-mono text-sm">{thingy.published_revision}</code>
            ) : (
              "None yet"
            )}
          </dd>
          <dt className="text-muted-foreground">Owner</dt>
          <dd className="m-0 min-w-0 break-all">{thingy.owner_id}</dd>
          {content !== undefined && (
            <>
              <dt className="text-muted-foreground">Can</dt>
              <dd className="m-0 min-w-0">
                {content.capabilities.length === 0 ? (
                  "Show its page only"
                ) : (
                  <ul
                    className="m-0 flex list-none flex-wrap gap-1.5 p-0"
                    aria-label="Capabilities"
                  >
                    {content.capabilities.map((capability) => (
                      <li
                        key={capability}
                        className="rounded-full border border-border bg-muted/50 px-2 py-0.5 text-sm text-muted-foreground"
                      >
                        {CAPABILITY_LABELS[capability] ?? capability}
                      </li>
                    ))}
                  </ul>
                )}
              </dd>
            </>
          )}
        </dl>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Done
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Share and the ⋯ menu (Share where there's little room, Thingy details, Delete thingy). */
export function ThingyActions({
  thingy,
  content,
  onDeleted,
  shareInMenu,
}: {
  thingy: Thingy;
  /** The published revision's content, for the capabilities in the details. */
  content?: ThingyContent;
  onDeleted: () => void;
  /** Offer Share in the menu instead of as a button, where the bar has little room. */
  shareInMenu: boolean;
}) {
  const info = useServerInfo();
  const [shareOpen, setShareOpen] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const canShare = thingy.permissions.manage && thingySharingAvailable(info);
  return (
    <>
      {canShare && !shareInMenu && (
        // share-button-glassy (index.css) paints the session header Share button's look.
        <Button
          type="button"
          aria-label="Share thingy"
          componentId="thingies.share"
          className="share-button-glassy h-6 shrink-0 gap-1 rounded-[6px] px-2 text-ui font-normal text-white"
          onClick={() => setShareOpen(true)}
        >
          <span className="flex size-4 shrink-0 items-center justify-center">
            <UserPlusIcon />
          </span>
          Share
        </Button>
      )}
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="More thingy actions"
            className={TOOLBAR_ICON_BUTTON}
          >
            <EllipsisVerticalIcon className="size-3.5" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="min-w-40">
          {canShare && shareInMenu && (
            <DropdownMenuItem onSelect={() => setShareOpen(true)}>
              <UserPlusIcon className="size-4" />
              Share
            </DropdownMenuItem>
          )}
          <DropdownMenuItem onSelect={() => setDetailsOpen(true)}>Thingy details</DropdownMenuItem>
          {thingy.permissions.manage && (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuItem variant="destructive" onSelect={() => setDeleting(true)}>
                Delete thingy
              </DropdownMenuItem>
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
      {canShare && (
        <PermissionsModal thingy={thingy} open={shareOpen} onOpenChange={setShareOpen} />
      )}
      <ThingyDetailsDialog
        thingy={thingy}
        content={content}
        open={detailsOpen}
        onOpenChange={setDetailsOpen}
      />
      {deleting && (
        <DeleteThingyDialog
          thingy={thingy}
          onClose={() => setDeleting(false)}
          onDeleted={onDeleted}
        />
      )}
    </>
  );
}
