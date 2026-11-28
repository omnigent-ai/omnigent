// A comment card's body while it's being changed: the editor in its place, what GitHub said
// about the last change, and the confirmations before deleting or discarding. Every card
// renderer wraps its body in this, so they all edit and delete the same way.

import type { ReactNode } from "react";
import { AlertTriangleIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { MarkdownComposer } from "@/markdownEditor/MarkdownComposer";
import { PullRequestMarkdownPreview } from "@/newPullRequestPage/shell/PullRequestDetails";
import type { CommentEditing } from "./usePullRequestCommentEditing";

function Strip({
  tone,
  children,
  actions,
}: {
  tone: "warning" | "muted";
  children: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div
      role="status"
      className={
        tone === "warning"
          ? "flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md bg-warning/10 px-2.5 py-1.5 text-ui text-foreground"
          : "flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md bg-muted/50 px-2.5 py-1.5 text-ui text-muted-foreground"
      }
    >
      {tone === "warning" && (
        <AlertTriangleIcon aria-hidden className="size-3.5 shrink-0 text-warning" />
      )}
      <span className="min-w-0 flex-1">{children}</span>
      {actions}
    </div>
  );
}

/**
 * `children` is the comment's body as the card shows it. While the comment is being edited
 * the editor replaces it; once the comment was deleted on GitHub, the text kept from it does.
 */
export function PullRequestCommentEditSurface({
  editing,
  onOpenOnGitHub,
  children,
}: {
  editing: CommentEditing;
  /** Opens the comment on GitHub, where there's a link to it. */
  onOpenOnGitHub?: () => void;
  children: ReactNode;
}): ReactNode {
  const { actions, entry } = editing;
  if (!actions) return children;
  const saving = actions.saving;
  const unconfirmed = entry.unknown;
  const kept = entry.tombstone;
  return (
    <>
      {kept ? (
        <div className="flex flex-col gap-2">
          <Strip
            tone="warning"
            actions={
              <>
                <Button type="button" variant="ghost" size="sm" onClick={actions.copyKept}>
                  Copy Text
                </Button>
                <Button type="button" variant="ghost" size="sm" onClick={actions.discardKept}>
                  Discard
                </Button>
              </>
            }
          >
            This comment was deleted on GitHub. Your edit wasn’t saved.
          </Strip>
          <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border p-2 font-sans text-ui text-muted-foreground">
            {kept.text}
          </pre>
        </div>
      ) : entry.editing ? (
        <div className="flex flex-col gap-2">
          {entry.conflict && (
            <Strip
              tone="warning"
              actions={
                <>
                  <Button type="button" variant="ghost" size="sm" onClick={actions.keepMine}>
                    Keep Mine
                  </Button>
                  <Button type="button" size="sm" onClick={actions.loadLatest}>
                    Load Latest
                  </Button>
                </>
              }
            >
              This comment changed on GitHub while you were editing.
            </Strip>
          )}
          <MarkdownComposer
            value={entry.editing.text}
            onChange={editing.setText}
            onSubmit={actions.save}
            onCancel={actions.cancel}
            submitLabel="Save Comment Edit"
            submitVerb="save"
            size="comment"
            label="Edit comment"
            placeholder="Leave a comment"
            // While a change is out or unconfirmed, nothing here may change or send it.
            disabled={saving || !!unconfirmed}
            autoFocus
            preview={<PullRequestMarkdownPreview body={entry.editing.text} />}
            attachments={null}
            footerActions={
              <>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={saving || !!unconfirmed}
                  onClick={actions.cancel}
                >
                  Cancel
                </Button>
                <Button type="button" size="sm" disabled={!actions.saveable} onClick={actions.save}>
                  {saving ? "Saving…" : "Save"}
                </Button>
              </>
            }
          />
        </div>
      ) : (
        children
      )}
      {entry.pending?.change === "delete" && <Strip tone="muted">Deleting…</Strip>}
      {unconfirmed && (
        <Strip
          tone="warning"
          actions={
            <>
              {actions.canCheck && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={actions.checking}
                  onClick={actions.checkAgain}
                >
                  {actions.checking ? "Checking…" : "Check Again"}
                </Button>
              )}
              {onOpenOnGitHub && (
                <Button type="button" variant="ghost" size="sm" onClick={onOpenOnGitHub}>
                  Open on GitHub
                </Button>
              )}
              {actions.hasKeptText && !entry.editing && (
                <>
                  <Button type="button" variant="ghost" size="sm" onClick={actions.copyKept}>
                    Copy Text
                  </Button>
                  <Button type="button" variant="ghost" size="sm" onClick={actions.discardKept}>
                    Discard
                  </Button>
                </>
              )}
            </>
          }
        >
          {entry.checkFailed
            ? "GitHub didn’t confirm this change, and the comment couldn’t be read just now."
            : "GitHub didn’t confirm this change."}
        </Strip>
      )}
      {entry.message && !unconfirmed && (
        <p role="alert" className="text-ui text-destructive">
          {entry.message}
        </p>
      )}
      <Dialog open={editing.deleteAsked} onOpenChange={editing.setDeleteAsked}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete this comment?</DialogTitle>
            <DialogDescription>
              It’s removed from the pull request on GitHub. This can’t be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => editing.setDeleteAsked(false)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={editing.confirmDelete}>
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={editing.discardAsked} onOpenChange={editing.setDiscardAsked}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Discard your changes to this comment?</DialogTitle>
            <DialogDescription>
              {unconfirmed
                ? "GitHub may already have this edit. Check Again still checks the comment."
                : "Your edit isn’t saved anywhere else."}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => editing.setDiscardAsked(false)}>
              Keep Editing
            </Button>
            <Button variant="destructive" onClick={editing.confirmDiscard}>
              Discard
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
