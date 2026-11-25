// A selection's Open in Workspace Sidebar, Open on GitHub, Copy Links and Copy Branch Names, with
// what they do: opening each PR where a single one opens, asking first for many, saying which
// ones a browser blocked, and copying with a toast that says whether it worked.

import { useCallback, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { copyText } from "@/lib/clipboard";
import { useEnabledReviewSite } from "@/hooks/usePullRequestReviewSite";
import { openLinkWithOutcome } from "@/lib/openLinkInApp";
import type { PullRequestListEntry } from "@/shell/PullRequestListRows";

import type { ListSelectionAction } from "./listSelectionActions";
import {
  type SelectionCopy,
  type SelectionLink,
  selectionLinkActions,
} from "./selectionLinkActions";

// Each batch's blocked notice is its own, kept across that batch's retries only.
let blockedBatches = 0;

const pluralPullRequests = (count: number) => `${count} pull request${count === 1 ? "" : "s"}`;

export function copySelection({ text, copied, failed }: SelectionCopy): void {
  void copyText(text).then(
    () => toast.success(copied),
    () => toast.error(failed),
  );
}

/** Says the browser blocked `blocked` of `total`, and how to try again. */
export function blockedMessage(blocked: number, total: number): string {
  const what =
    blocked === total
      ? total === 1
        ? "Your browser blocked the pull request from opening."
        : `Your browser blocked all ${total} pull requests from opening.`
      : `Your browser blocked ${blocked} of ${pluralPullRequests(total)} from opening.`;
  return `${what} Allow pop-ups for this site, then ${blocked === total ? "try again" : "open the rest"}.`;
}

/**
 * Open `links` now, each as a single PR's Open on GitHub does. Call it within the click that
 * asked; where a browser blocks some, a toast offers to open or copy only those. A retry passes
 * its batch's toast, which it updates or, once all open, dismisses.
 */
export function openSelectionLinks(
  conversationId: string,
  links: readonly SelectionLink[],
  retrying?: string,
): void {
  const blocked = links.filter(
    (link) => openLinkWithOutcome(conversationId, link.href) === "blocked",
  );
  if (blocked.length === 0) {
    if (retrying) toast.dismiss(retrying);
    return;
  }
  const toastId = retrying ?? `pull-requests-open-blocked-${++blockedBatches}`;
  toast.error(blockedMessage(blocked.length, links.length), {
    id: toastId,
    duration: Infinity,
    action: {
      label: blocked.length === links.length ? "Try Again" : "Open the Rest",
      onClick: (event) => {
        // The retry settles the toast itself; blocked again, it must stay.
        event.preventDefault();
        openSelectionLinks(conversationId, blocked, toastId);
      },
    },
    cancel: {
      label: "Copy Their Links",
      onClick: () =>
        copySelection({
          text: blocked.map((link) => link.href).join("\n"),
          copied: `Copied ${blocked.length === 1 ? "its link" : `${blocked.length} links`}`,
          failed: "Couldn’t copy the links",
        }),
    },
  });
}

interface OpenRequest {
  links: readonly SelectionLink[];
  title: string;
  conversationId: string;
  scope: string;
}

/** `scope` is the list's selection scope: a confirmation asked in another scope is dropped. */
export function useSelectionLinkActions(
  conversationId: string,
  scope: string = conversationId,
  /** Opens each PR in the workspace sidebar, where the list can. */
  openInWorkspace?: (urls: readonly string[]) => void,
): {
  actionsFor: (selected: readonly PullRequestListEntry[]) => ListSelectionAction[];
  dialog: React.ReactNode;
} {
  const reviewSite = useEnabledReviewSite();
  // The links as they were when it asked, whatever the selection does behind the dialog.
  const [request, setRequest] = useState<OpenRequest | null>(null);
  if (request && request.scope !== scope) setRequest(null);
  const actionsFor = useCallback(
    (selected: readonly PullRequestListEntry[]) =>
      selectionLinkActions(selected, {
        reviewSite,
        open: (links) => openSelectionLinks(conversationId, links),
        confirmOpen: (links, title) => setRequest({ links, title, conversationId, scope }),
        copy: copySelection,
        openInWorkspace,
      }),
    [conversationId, scope, reviewSite, openInWorkspace],
  );
  const dialog = (
    <Dialog open={!!request} onOpenChange={(open) => !open && setRequest(null)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{request?.title}</DialogTitle>
          <DialogDescription>Each one opens in its own tab.</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={() => setRequest(null)}>
            Cancel
          </Button>
          <Button
            onClick={() => {
              // Opened within this click, which a browser's pop-up blocker allows.
              if (request) openSelectionLinks(request.conversationId, request.links);
              setRequest(null);
            }}
          >
            Open {request ? request.links.length : ""}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
  return { actionsFor, dialog };
}
