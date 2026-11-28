// Changes to comments that the pull request's lists no longer show: an edit or delete GitHub
// didn't confirm, text kept after a delete, or an unsaved edit. Each card shows its own; this
// keeps the rest in reach, in Comments and in ⌘K, until the person settles or drops them.
// A comment missing from a list proves nothing about it, so nothing here clears a hold.

import { useId, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { AlertTriangleIcon, CopyIcon, RefreshCwIcon, Trash2Icon } from "lucide-react";
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
import { useWorkspaceDraftOwner } from "@/lib/workspaceDraftOwner";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";
import { checkCommentChange, commentCheckReady } from "./commentChangeCheck";
import { commentEditVisible } from "./commentEditScope";
import {
  type ListedCommentEdit,
  discardLocalText,
  keptText,
  needsRecovery,
  updateCommentEdit,
  useCommentEditsFor,
} from "./commentEditStore";

const NO_COMMANDS: PaletteCommand[] = [];

const MESSAGES = {
  pending: "A change to a comment that isn’t listed here is still being sent.",
  unknown: "GitHub didn’t confirm a change to a comment that isn’t listed here.",
  kept: "A comment you were editing was deleted on GitHub. Your edit wasn’t saved.",
  draft: "You were editing a comment that isn’t listed here now. Your edit wasn’t saved.",
} as const;

function copyKept(text: string) {
  void copyText(text).then(
    () => toast.success("Text copied"),
    () => toast.error("Couldn’t copy the text"),
  );
}

/**
 * The workspace user's changes to `prUrl`'s comments that none of `listedIds` names, as
 * `sessionId`'s view may show them.
 */
export function useUnlistedCommentEdits(
  sessionId: string,
  prUrl: string | null,
  listedIds: ReadonlySet<string>,
): ListedCommentEdit[] {
  const owner = useWorkspaceDraftOwner();
  const edits = useCommentEditsFor(owner, prUrl);
  return useMemo(
    () =>
      edits.filter(
        ({ commentId, entry }) =>
          needsRecovery(entry) &&
          !listedIds.has(commentId) &&
          commentEditVisible(entry, prUrl, sessionId),
      ),
    [edits, listedIds, prUrl, sessionId],
  );
}

/**
 * The list, at the top of Comments, and its ⌘K rows while `active`. `listedIds` names every
 * comment the PR's loaded reads list, so each of those keeps its own card instead.
 */
export function PullRequestCommentRecoveries({
  conversationId,
  prUrl,
  listedIds,
  active,
}: {
  conversationId: string;
  prUrl: string | null;
  listedIds: ReadonlySet<string>;
  active: boolean;
}) {
  const queryClient = useQueryClient();
  const owner = useWorkspaceDraftOwner();
  const unlisted = useUnlistedCommentEdits(conversationId, prUrl, listedIds);
  // The entry Discard was opened for, with the workspace user and PR it belongs to: once
  // either changes, the dialog closes, and its confirmation never reaches another's text.
  const [asked, setAsked] = useState<{ key: string; owner: string; prUrl: string | null } | null>(
    null,
  );
  // Another workspace user or PR ends the request for good; coming back doesn't reopen it.
  if (asked && (asked.owner !== owner || asked.prUrl !== prUrl)) setAsked(null);
  const discarding =
    asked &&
    asked.owner === owner &&
    asked.prUrl === prUrl &&
    unlisted.some(({ key }) => key === asked.key)
      ? asked.key
      : null;
  const setDiscarding = (key: string | null) =>
    setAsked(key === null ? null : { key, owner, prUrl });
  const check = (key: string) => checkCommentChange(queryClient, conversationId, key);

  const commands = useMemo<PaletteCommand[]>(() => {
    if (!active || unlisted.length === 0) return NO_COMMANDS;
    return unlisted.flatMap(({ key, entry }) => {
      const rows: PaletteCommand[] = [];
      const description = "A comment that isn’t listed here";
      if (entry.unknown && commentCheckReady(entry)) {
        rows.push({
          id: `pull-request-comment-recovery:${key}:check`,
          label: "Check Comment Again",
          description,
          icon: RefreshCwIcon,
          keywords: ["comment", "check", "again", "pull request"],
          run: () => check(key),
        });
      }
      const text = keptText(entry);
      if (text !== null && !entry.pending) {
        rows.push({
          id: `pull-request-comment-recovery:${key}:copy`,
          label: "Copy Unsaved Comment Text",
          description,
          icon: CopyIcon,
          keywords: ["comment", "copy", "unsaved", "text", "pull request"],
          run: () => copyKept(text),
        });
        rows.push({
          id: `pull-request-comment-recovery:${key}:discard`,
          label: "Discard Unsaved Comment Text…",
          description,
          icon: Trash2Icon,
          keywords: ["comment", "discard", "unsaved", "text", "pull request"],
          movesFocus: true,
          run: () => setDiscarding(key),
        });
      }
      return rows;
    });
    // The rows run what's current through `check` and the store; the list describes them.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, unlisted]);
  const instance = useId();
  usePaletteCommands(`pull-request-comment-recovery:${instance}`, "Comment", commands);

  if (unlisted.length === 0) return null;
  const discardingEntry = unlisted.find(({ key }) => key === discarding)?.entry ?? null;
  return (
    <section aria-label="Changes to comments not listed here" className="flex flex-col gap-2 p-3">
      {unlisted.map(({ key, entry }) => {
        const state = needsRecovery(entry)!;
        const text = keptText(entry);
        return (
          <div
            key={key}
            role="group"
            aria-label={MESSAGES[state]}
            className="flex flex-col gap-2 rounded-md border border-border bg-warning/10 p-2.5 text-ui"
          >
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <AlertTriangleIcon aria-hidden className="size-3.5 shrink-0 text-warning" />
              <span className="min-w-0 flex-1">
                {MESSAGES[state]}
                {state === "unknown" && entry.checkFailed && (
                  <> The comment couldn’t be read just now.</>
                )}
              </span>
              {entry.unknown && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={entry.checking !== null}
                  onClick={() => check(key)}
                >
                  {entry.checking !== null ? "Checking…" : "Check Again"}
                </Button>
              )}
              {text !== null && !entry.pending && (
                <>
                  <Button type="button" variant="ghost" size="sm" onClick={() => copyKept(text)}>
                    Copy Text
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => setDiscarding(key)}
                  >
                    Discard
                  </Button>
                </>
              )}
            </div>
            {text !== null && (
              <pre className="max-h-32 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-background p-2 font-sans text-muted-foreground">
                {text}
              </pre>
            )}
          </div>
        );
      })}
      <Dialog open={discarding !== null} onOpenChange={(open) => !open && setDiscarding(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Discard this text?</DialogTitle>
            <DialogDescription>
              {discardingEntry?.unknown
                ? "GitHub may already have this edit. Check Again still checks the comment."
                : "It isn’t saved anywhere else."}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDiscarding(null)}>
              Keep It
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                // Checked again at the click: still this user's entry for this PR, still listed here.
                if (discarding && unlisted.some(({ key }) => key === discarding)) {
                  updateCommentEdit(discarding, discardLocalText);
                }
                setDiscarding(null);
              }}
            >
              Discard
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
