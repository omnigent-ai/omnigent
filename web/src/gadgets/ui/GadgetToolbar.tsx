// A gadget's controls in its side panel tab: its inline-editable name, Pin, Comments, Share and a
// ⋯ menu with Gadget details and Delete. In full screen they move into the panel's top bar.
import { type KeyboardEvent, type RefObject, useEffect, useRef, useState } from "react";
import {
  CheckIcon,
  CopyIcon,
  EllipsisVerticalIcon,
  MessageSquareIcon,
  PinIcon,
  UserPlusIcon,
} from "lucide-react";
import { toast } from "sonner";

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
import { copyText } from "@/lib/clipboard";
import { gadgetSharingAvailable } from "@/lib/permissionsApi";
import { useRebasePath } from "@/lib/routing";
import { cn } from "@/lib/utils";
import type { Gadget, GadgetContent } from "@/gadgets/api/gadgetApi";
import { DeleteGadgetDialog } from "./DeleteGadgetDialog";
import { MAX_GADGET_NAME_LENGTH } from "./GadgetTemplateGallery";
import { gadgetErrorMessage, gadgetLink } from "./gadgetNavigation";
import { usePinGadget, useRenameGadget, useGadgetShelf } from "./gadgetQueries";
import type { GadgetComments } from "./useGadgetComments";

/**
 * The gadget's name. A viewer who can edit it clicks the name to rename it in place, as a
 * session's title in its header: Enter or leaving the field saves, Escape cancels.
 */
export function GadgetName({ gadget }: { gadget: Gadget }) {
  const rename = useRenameGadget();
  const [draft, setDraft] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (!gadget.permissions.publish) {
    return (
      <span className="min-w-0 truncate text-ui font-medium" title={gadget.name}>
        {gadget.name}
      </span>
    );
  }
  const save = () => {
    if (draft === null || rename.isPending) return;
    const name = draft.trim();
    if (!name || name === gadget.name) {
      setDraft(null);
      setError(null);
      return;
    }
    if (name.length > MAX_GADGET_NAME_LENGTH) {
      setError(`Use at most ${MAX_GADGET_NAME_LENGTH} characters.`);
      return;
    }
    setError(null);
    rename.mutate(
      { gadgetId: gadget.id, name },
      {
        onSuccess: () => setDraft(null),
        onError: (renameError) => setError(`Couldn’t rename: ${gadgetErrorMessage(renameError)}`),
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
              aria-label={`Rename ${gadget.name}`}
              onClick={() => {
                setError(null);
                setDraft(gadget.name);
              }}
              // The negative margin lines the name up with the text around it, not its outline.
              className="-mx-[5px] min-w-0 cursor-text truncate rounded-md border border-transparent px-1 text-left text-ui font-medium hover:border-border"
            >
              {gadget.name}
            </button>
          </TooltipTrigger>
          <TooltipContent side="bottom">Rename</TooltipContent>
        </Tooltip>
      ) : (
        <input
          aria-label="Gadget name"
          aria-invalid={error !== null || undefined}
          // The field replaces the name the viewer just clicked.
          autoFocus
          value={draft}
          maxLength={MAX_GADGET_NAME_LENGTH}
          disabled={rename.isPending}
          onFocus={(event) => event.currentTarget.select()}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKeyDown}
          onBlur={save}
          className="-mx-[5px] h-6 min-w-0 rounded-md border border-ring bg-background px-1 text-ui font-medium outline-none"
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

// Phones get the shell's 44 px touch floor.
const TOOLBAR_ICON_BUTTON =
  "size-6 shrink-0 text-muted-foreground hover:text-foreground mobile-touch-target";

/** Pins the gadget for the viewer, so the sidebar lists it under Gadget. */
export function GadgetPinButton({ gadget }: { gadget: Pick<Gadget, "id"> }) {
  const shelf = useGadgetShelf();
  const pin = usePinGadget();
  const pinned = shelf.data?.pinned.some((item) => item.id === gadget.id) ?? false;
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
          componentId="gadgets.pin"
          onClick={() => pin.mutate({ gadgetId: gadget.id, pinned: !pinned })}
        >
          {/* Filled while pinned, as a pressed toggle. */}
          <PinIcon className={cn("size-4", pinned && "fill-current text-foreground")} />
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom">
        {pin.isError ? `Couldn’t change the pin: ${gadgetErrorMessage(pin.error)}` : label}
      </TooltipContent>
    </Tooltip>
  );
}

/** Shows or hides the comments beside the gadget, with the number of open ones. */
export function GadgetCommentsButton({ comments }: { comments: GadgetComments }) {
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
            "h-6 min-w-6 shrink-0 px-1 text-muted-foreground hover:text-foreground mobile-touch-target",
            comments.open && "bg-muted text-foreground",
          )}
          componentId="gadgets.comments"
          onClick={() => comments.setOpen(!comments.open)}
        >
          <MessageSquareIcon className="size-4" />
          {count > 0 && (
            <span className="rounded-full bg-muted px-1.5 text-[10px] tabular-nums">{count}</span>
          )}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom">
        <span className="flex flex-col items-start">
          <span>Comments</span>
          {comments.canComment && (
            <span className="opacity-70">
              Select something in the gadget and choose Add comment
            </span>
          )}
        </span>
      </TooltipContent>
    </Tooltip>
  );
}

const CAPABILITY_LABELS: Record<string, string> = {
  "data.read": "Read its data",
  "data.write": "Change its data",
  "sessions.read": "Read your sessions",
};

/** The gadget's link, with Copy. */
function GadgetLinkField({ gadgetId }: { gadgetId: string }) {
  const rebasePath = useRebasePath();
  const link = gadgetLink(gadgetId, rebasePath);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return undefined;
    const timer = setTimeout(() => setCopied(false), 2000);
    return () => clearTimeout(timer);
  }, [copied]);
  return (
    <span className="flex min-w-0 items-center gap-1">
      <span className="min-w-0 truncate text-muted-foreground" title={link}>
        {link}
      </span>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label={copied ? "Link copied" : "Copy link"}
            className={TOOLBAR_ICON_BUTTON}
            componentId="gadgets.details.copy_link"
            onClick={() =>
              void copyText(link).then(
                () => setCopied(true),
                () => toast.error("Couldn’t copy the link"),
              )
            }
          >
            {copied ? <CheckIcon className="size-3.5" /> : <CopyIcon className="size-3.5" />}
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">{copied ? "Copied" : "Copy link"}</TooltipContent>
      </Tooltip>
    </span>
  );
}

/** Hands focus back to the ⋯ button a menu dialog was opened from, whose menu item is gone. */
function focusTrigger(trigger: RefObject<HTMLElement | null>) {
  return (event: Event) => {
    if (!trigger.current?.isConnected) return;
    event.preventDefault();
    trigger.current.focus();
  };
}

function GadgetDetailsDialog({
  gadget,
  content,
  open,
  onOpenChange,
  onCloseAutoFocus,
}: {
  gadget: Gadget;
  content: GadgetContent | undefined;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCloseAutoFocus?: (event: Event) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md" onCloseAutoFocus={onCloseAutoFocus}>
        <DialogHeader>
          <DialogTitle>Gadget details</DialogTitle>
          <DialogDescription className="break-words">{gadget.name}</DialogDescription>
        </DialogHeader>
        <dl className="m-0 grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-3 text-ui">
          <dt className="text-muted-foreground">Status</dt>
          <dd className="m-0 min-w-0">
            {gadget.published_revision ? "Published" : "Not published yet"}
          </dd>
          <dt className="text-muted-foreground">Owner</dt>
          <dd className="m-0 min-w-0 break-all">
            {gadget.owner_id === gadget.viewer_id ? "You" : gadget.owner_id}
          </dd>
          <dt className="text-muted-foreground">Link</dt>
          <dd className="m-0 min-w-0">
            <GadgetLinkField gadgetId={gadget.id} />
          </dd>
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

/** Share and the ⋯ menu (Share where there's little room, Gadget details, Delete gadget). */
export function GadgetActions({
  gadget,
  content,
  onDeleted,
  shareInMenu,
}: {
  gadget: Gadget;
  /** The published revision's content, for the capabilities in the details. */
  content?: GadgetContent;
  onDeleted: () => void;
  /** Offer Share in the menu instead of as a button, where the bar has little room. */
  shareInMenu: boolean;
}) {
  const info = useServerInfo();
  const [shareOpen, setShareOpen] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const menuTriggerRef = useRef<HTMLButtonElement>(null);
  const canShare = gadget.permissions.manage && gadgetSharingAvailable(info);
  return (
    <>
      {canShare && !shareInMenu && (
        // share-button-glassy (index.css) paints the session header Share button's look.
        <Button
          type="button"
          aria-label="Share gadget"
          componentId="gadgets.share"
          className="share-button-glassy mobile-touch-target h-6 shrink-0 gap-1 rounded-[6px] px-2 text-ui font-normal text-white"
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
            ref={menuTriggerRef}
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="More gadget actions"
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
          <DropdownMenuItem onSelect={() => setDetailsOpen(true)}>Gadget details</DropdownMenuItem>
          {gadget.permissions.manage && (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuItem variant="destructive" onSelect={() => setDeleting(true)}>
                Delete gadget
              </DropdownMenuItem>
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
      {canShare && (
        <PermissionsModal gadget={gadget} open={shareOpen} onOpenChange={setShareOpen} />
      )}
      <GadgetDetailsDialog
        gadget={gadget}
        content={content}
        open={detailsOpen}
        onOpenChange={setDetailsOpen}
        onCloseAutoFocus={focusTrigger(menuTriggerRef)}
      />
      {deleting && (
        <DeleteGadgetDialog
          gadget={gadget}
          onClose={() => setDeleting(false)}
          onDeleted={onDeleted}
          onCloseAutoFocus={focusTrigger(menuTriggerRef)}
        />
      )}
    </>
  );
}
