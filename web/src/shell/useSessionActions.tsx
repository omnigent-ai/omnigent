// The session row actions that open a dialog or run in the background (share,
// fork, automation, stop, archive, delete, leave), shared by the sidebar row and
// the project board's cards so both menus confirm and act the same way.

import { useState, type ReactNode } from "react";
import { GitBranchIcon } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { PermissionsModal } from "@/components/PermissionsModal";
import { AutomationSourceDeleteNotice } from "@/components/scheduled/AutomationSourceDeleteNotice";
import { CreateAutomationFromSessionDialog } from "@/components/scheduled/CreateAutomationFromSessionDialog";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { showToast } from "@/components/ui/toast";
import {
  type Conversation,
  useArchiveConversation,
  useLeaveSession,
  useStopAndDeleteConversation,
  useStopSession,
} from "@/hooks/useConversations";
import { automationDeletedWith, useDeleteSourceAutomations } from "@/lib/automationFromSession";
import type { useServerInfo } from "@/lib/CapabilitiesContext";
import { useNavigate } from "@/lib/routing";
import { useArchivePrompt } from "./ArchivePrompt";
import { showArchiveUndoToast } from "./archiveUndoToast";
import { ForkSessionDialog } from "./ForkSessionDialog";
import { isOpenSession } from "./openSession";
import type { SessionLeaveCause } from "./sessionCommands/archiveNavigation";

export interface SessionActions {
  setShareOpen: (open: boolean) => void;
  setForkOpen: (open: boolean) => void;
  setAutomationOpen: (open: boolean) => void;
  setStopOpen: (open: boolean) => void;
  setDeleteOpen: (open: boolean) => void;
  setLeaveOpen: (open: boolean) => void;
  /** Archives, after the worktree prompt, or unarchives an archived session. */
  runArchive: () => void;
  runUnarchive: () => void;
  /** The dialogs these actions open; render them next to the menu. */
  dialogs: ReactNode;
}

/**
 * Dialog state and handlers for one session's row actions.
 *
 * :param conversation: The session the actions apply to.
 * :param label: Its display title, quoted in the confirmations.
 * :param viewerId: The signed-in user's id, which Leave revokes.
 * :param serverInfo: Server capabilities, for the automation delete notice.
 * :param onLeave: Called right before the session is deleted, archived, or
 *     left, with which one, e.g. to navigate off a view that shows it.
 * :param componentIdPrefix: Analytics prefix for the confirm buttons, e.g.
 *     ``"sidebar.conversation"``.
 * :param reportsAfterUnmount: Whether a Leave or Stop still reports how it went once the
 *     caller has unmounted, as ⌘K's action host needs: it can be replaced before they settle.
 *     A Leave then reads the open session from ``OpenSessionPublisher``, so mount it too.
 */
export function useSessionActions({
  conversation,
  label,
  viewerId,
  serverInfo,
  onLeave,
  componentIdPrefix,
  reportsAfterUnmount = false,
}: {
  conversation: Conversation;
  label: string;
  viewerId: string | null;
  serverInfo: ReturnType<typeof useServerInfo>;
  onLeave?: (cause: SessionLeaveCause) => void;
  componentIdPrefix: string;
  reportsAfterUnmount?: boolean;
}): SessionActions {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const del = useStopAndDeleteConversation();
  const archive = useArchiveConversation();
  const archivePrompt = useArchivePrompt();
  const leave = useLeaveSession();
  // The user-facing "Stop session" action. Archiving does NOT go through here —
  // the server stops the session itself once the archived flag commits, so a
  // hidden session never keeps a runner alive.
  const stopSession = useStopSession();
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [stopOpen, setStopOpen] = useState(false);
  // Opt-in "delete local branch" checkbox (worktree sessions only).
  const [deleteBranch, setDeleteBranch] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [forkOpen, setForkOpen] = useState(false);
  const [automationOpen, setAutomationOpen] = useState(false);
  const [leaveOpen, setLeaveOpen] = useState(false);
  const isArchived = conversation.archived === true;
  const gitBranch = conversation.git_branch ?? null;
  // Deleting a session an automation runs from deletes the automation first.
  const automationSourceOf = automationDeletedWith(serverInfo, conversation);
  const deleteSourceAutomations = useDeleteSourceAutomations();

  function confirmDelete() {
    // Fire-and-forget: close the dialog and drop the row immediately so the
    // user isn't blocked on the (potentially slow) DELETE — server-side
    // teardown can take seconds. The mutation removes the row from the
    // cached lists optimistically, which unmounts the caller, so anything
    // that must happen on delete either runs here or lives in the hook (a
    // mutate-level callback would never fire).
    setDeleteOpen(false);
    setDeleteBranch(false);
    if (automationSourceOf !== null) {
      void deleteWithAutomation(automationSourceOf, gitBranch !== null && deleteBranch);
      return;
    }
    // Leave a view showing the session now, so it doesn't sit on an id that's about to 404.
    onLeave?.("delete");
    del.mutate({ id: conversation.id, deleteBranch: gitBranch !== null && deleteBranch });
  }

  // "Delete both": the automation first, so no run starts from a missing session.
  async function deleteWithAutomation(taskId: string, deleteLocalBranch: boolean) {
    if (!(await deleteSourceAutomations([taskId], [conversation.id]))) {
      showToast("Couldn't delete the automation, so the session was kept");
      return;
    }
    // A caller replaced while the automation deleted can't see the route, so the open session
    // is read live: leave it only if it's still the one shown.
    if (reportsAfterUnmount && isOpenSession(conversation.id)) onLeave?.("delete");
    del.mutate({ id: conversation.id, deleteBranch: deleteLocalBranch });
  }

  function runArchive() {
    if (isArchived) {
      runUnarchive();
      return;
    }
    archivePrompt.requestArchive([conversation], (deleteWorktreeIds) =>
      archiveNow(deleteWorktreeIds.has(conversation.id)),
    );
  }

  function archiveNow(deleteWorktree: boolean) {
    // The archive PATCH sends only the flag: the server stops the session (and
    // tears down a host-spawned runner) in the background once it's committed.
    // A client stop too would race that one against the same runner, and the
    // loser gets a 503 from the already-killed pane.
    //
    // The row leaves its list optimistically (useArchiveConversation flips the
    // cached `archived` flag in onMutate), so the caller unmounts on the next
    // frame. Leaving the archived session's view therefore has to happen HERE,
    // synchronously — not in an onSuccess callback that fires a round-trip
    // later with a stale view. Mirrors confirmDelete.
    onLeave?.("archive");
    archive.mutate({ id: conversation.id, archived: true, deleteWorktree });
    // Offer an Undo (and point at where the session went) — fire NOW, not in a
    // mutate onSuccess: per-call mutate callbacks don't fire once their observer
    // unmounts. A failed archive reconciles the row back with its own error
    // toast. The toast is driven imperatively (module state + app-level
    // Toaster), so it survives the caller unmounting.
    showArchiveUndoToast(queryClient, [conversation], navigate);
  }

  function runUnarchive() {
    archive.mutate({ id: conversation.id, archived: !isArchived });
  }

  function confirmLeave() {
    // Leave is a self-revoke, so it needs the viewer's own id. The menu item is
    // gated on the row NOT being owned by the viewer, which is only decidable
    // once the id has resolved — so this is non-null wherever it's reachable.
    if (viewerId === null) return;
    // Close immediately — the row drops out of the list on success, so there's
    // nothing left to show progress against. A failure surfaces as a toast
    // (the row is still there to retry from).
    setLeaveOpen(false);
    const reportFailure = (err: unknown) => {
      const detail = err instanceof Error && err.message ? `: ${err.message}` : "";
      showToast(`Couldn't leave the session${detail}`);
    };
    if (!reportsAfterUnmount) {
      leave.mutate(
        { id: conversation.id, viewerId },
        // The session 404s for this user now, so don't leave them staring at it.
        { onSuccess: () => onLeave?.("leave"), onError: reportFailure },
      );
      return;
    }
    // The promise settles even once the caller has unmounted, which a per-call
    // mutate callback wouldn't.
    // A row's callback can't see a switch that unmounts it, so the open session is read live.
    const leftFromItsView = isOpenSession(conversation.id);
    leave.mutateAsync({ id: conversation.id, viewerId }).then(
      // The session 404s for this user now, so don't leave them staring at it, unless they've
      // already opened another one.
      () => {
        if (!leftFromItsView || isOpenSession(conversation.id)) onLeave?.("leave");
      },
      reportFailure,
    );
  }

  const dialogs = (
    <>
      {/* Mount only while open — one per row, its hook tree + JSX would
          otherwise run closed on every row re-render. */}
      {shareOpen && (
        <PermissionsModal
          sessionId={conversation.id}
          open={shareOpen}
          onOpenChange={setShareOpen}
        />
      )}
      {forkOpen && (
        <ForkSessionDialog
          sourceSessionId={conversation.id}
          sourceTitle={conversation.title}
          sourceWorkspace={conversation.workspace}
          sourceHostId={conversation.host_id}
          sourceGitBranch={conversation.git_branch}
          open
          onOpenChange={setForkOpen}
        />
      )}
      {automationOpen && (
        <CreateAutomationFromSessionDialog
          conversation={conversation}
          open
          onOpenChange={setAutomationOpen}
        />
      )}
      <Dialog
        open={deleteOpen}
        onOpenChange={(open) => {
          setDeleteOpen(open);
          // Reset the checkbox on close so it doesn't carry over.
          if (!open) setDeleteBranch(false);
        }}
      >
        {/* Body only while open — see the share modal above. */}
        {deleteOpen && (
          <DialogContent
            // Don't trigger the surrounding Link when the modal opens
            // — the dialog content is a portal, but defensively belt-
            // and-braces the click path.
            onClick={(e) => e.stopPropagation()}
          >
            <DialogHeader>
              <DialogTitle>
                {automationSourceOf !== null ? "Delete session and automation?" : "Delete session?"}
              </DialogTitle>
              <DialogDescription>
                <span className="font-medium break-all">{label}</span> and all of its history will
                be removed. This cannot be undone.
              </DialogDescription>
            </DialogHeader>
            {automationSourceOf !== null && (
              <AutomationSourceDeleteNotice
                taskId={automationSourceOf}
                onNavigate={() => {
                  setDeleteOpen(false);
                  setDeleteBranch(false);
                }}
              />
            )}
            {gitBranch !== null && (
              <div className="flex flex-col gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-3">
                <p className="text-sm text-muted-foreground">
                  Optionally clean up the git worktree. These actions are{" "}
                  <span className="font-semibold text-destructive">irreversible</span>.
                </p>
                <label className="flex cursor-pointer items-start gap-2 text-ui">
                  <input
                    type="checkbox"
                    data-testid="delete-branch-checkbox"
                    checked={deleteBranch}
                    onChange={(e) => setDeleteBranch(e.target.checked)}
                    className="mt-0.5 size-4 shrink-0 accent-destructive"
                  />
                  <GitBranchIcon className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
                  <span className="min-w-0">
                    Delete local branch{" "}
                    <code className="break-all rounded bg-muted px-1 py-0.5 text-sm">
                      {gitBranch}
                    </code>
                  </span>
                </label>
              </div>
            )}
            {/* Drop the default footer divider + muted bar so the actions
              blend into the dialog body (same background). */}
            <DialogFooter className="border-t-0 bg-transparent">
              <Button
                type="button"
                variant="ghost"
                onClick={() => setDeleteOpen(false)}
                disabled={del.isPending}
              >
                Cancel
              </Button>
              <Button
                type="button"
                variant="destructive"
                onClick={confirmDelete}
                disabled={del.isPending}
                componentId={`${componentIdPrefix}.delete`}
              >
                {automationSourceOf !== null ? "Delete both" : "Delete"}
              </Button>
            </DialogFooter>
          </DialogContent>
        )}
      </Dialog>
      {archivePrompt.dialog}
      <Dialog open={leaveOpen} onOpenChange={setLeaveOpen}>
        {leaveOpen && (
          <DialogContent
            // Keep dialog clicks off the surrounding Link (same defensive
            // handling as the delete dialog above).
            onClick={(e) => e.stopPropagation()}
          >
            <DialogHeader>
              <DialogTitle>Leave session?</DialogTitle>
              <DialogDescription>
                <span className="font-medium break-all">{label}</span> will be removed from your
                sidebar. Nothing is deleted — the session and its history stay with its owner, who
                can share it with you again.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter className="border-t-0 bg-transparent">
              <Button
                type="button"
                variant="ghost"
                onClick={() => setLeaveOpen(false)}
                disabled={leave.isPending}
              >
                Cancel
              </Button>
              <Button
                type="button"
                variant="destructive"
                data-testid="confirm-leave-conversation"
                onClick={confirmLeave}
                disabled={leave.isPending}
                componentId={`${componentIdPrefix}.leave`}
              >
                Leave
              </Button>
            </DialogFooter>
          </DialogContent>
        )}
      </Dialog>
      {/* The stale-error reset lives on the kebab item's onSelect (the only
          open path) — onOpenChange only fires for Radix-initiated closes. */}
      <Dialog open={stopOpen} onOpenChange={setStopOpen}>
        {stopOpen && (
          <DialogContent
            // Keep dialog clicks off the surrounding Link (same defensive
            // handling as the delete dialog above).
            onClick={(e) => e.stopPropagation()}
          >
            <DialogHeader>
              <DialogTitle>Stop session?</DialogTitle>
              <DialogDescription>
                This terminates the running session for <span className="font-medium">{label}</span>
                {conversation.host_id
                  ? " and stops its runner, including side chats running on it."
                  : "."}{" "}
                Conversation histories are kept.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button type="button" variant="ghost" onClick={() => setStopOpen(false)}>
                Cancel
              </Button>
              <Button
                type="button"
                variant="destructive"
                data-testid="stop-session-confirm"
                onClick={() => {
                  // Close now and stop in the background — keeping the modal open
                  // for the whole kill blocks the rest of the page. A failure
                  // surfaces as a toast since the dialog is already gone.
                  setStopOpen(false);
                  const reportFailure = (err: unknown) => {
                    const detail = err instanceof Error && err.message ? `: ${err.message}` : "";
                    showToast(`Couldn't stop the session${detail}`);
                  };
                  // As for Leave, the promise reports even once the caller has unmounted.
                  if (reportsAfterUnmount) {
                    stopSession.mutateAsync(conversation.id).catch(reportFailure);
                  } else {
                    stopSession.mutate(conversation.id, { onError: reportFailure });
                  }
                }}
                componentId={`${componentIdPrefix}.stop`}
              >
                Stop session
              </Button>
            </DialogFooter>
          </DialogContent>
        )}
      </Dialog>
    </>
  );

  return {
    setShareOpen,
    setForkOpen,
    setAutomationOpen,
    setStopOpen,
    setDeleteOpen,
    setLeaveOpen,
    runArchive,
    runUnarchive,
    dialogs,
  };
}
