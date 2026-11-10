import { useCallback, useId, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { Conversation } from "@/hooks/useConversations";
import { isSessionWorking } from "@/hooks/useSessionState";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  readDeleteWorktreesOnArchive,
  writeDeleteWorktreesOnArchive,
} from "@/lib/archiveWorktreePreferences";

/** Called with the ids whose worktree should be removed (empty = archive only). */
type ProceedFn = (deleteWorktreeIds: ReadonlySet<string>) => void;

interface PendingPrompt {
  worktreeCount: number;
  choose: (deleteWorktrees: boolean) => void;
}

interface PendingRunningPrompt {
  runningCount: number;
  sessionCount: number;
  confirm: () => void;
}

/**
 * Confirm archiving working sessions, then apply the worktree preference.
 * Callers render `dialog`; dismissing either prompt cancels the archive.
 */
export function useArchivePrompt(): {
  requestArchive: (conversations: readonly Conversation[], proceed: ProceedFn) => void;
  dialog: ReactNode;
} {
  const [pending, setPending] = useState<PendingPrompt | null>(null);
  const [pendingRunning, setPendingRunning] = useState<PendingRunningPrompt | null>(null);
  const serverInfo = useServerInfo();
  const cleanupSupported = serverInfo !== "loading" && serverInfo.archive_worktree_cleanup === true;

  const requestWorktreeChoice = useCallback(
    (conversations: readonly Conversation[], proceed: ProceedFn) => {
      const worktreeIds = new Set(
        conversations.filter((c) => c.git_branch != null).map((c) => c.id),
      );
      if (!cleanupSupported || worktreeIds.size === 0) {
        proceed(new Set());
        return;
      }
      const preference = readDeleteWorktreesOnArchive();
      if (preference !== null) {
        proceed(preference ? worktreeIds : new Set());
        return;
      }
      setPending({
        worktreeCount: worktreeIds.size,
        choose: (deleteWorktrees) => proceed(deleteWorktrees ? worktreeIds : new Set()),
      });
    },
    [cleanupSupported],
  );

  const requestArchive = useCallback(
    (conversations: readonly Conversation[], proceed: ProceedFn) => {
      const runningCount = conversations.filter(isSessionWorking).length;
      if (runningCount === 0) {
        requestWorktreeChoice(conversations, proceed);
        return;
      }
      setPendingRunning({
        runningCount,
        sessionCount: conversations.length,
        confirm: () => requestWorktreeChoice(conversations, proceed),
      });
    },
    [requestWorktreeChoice],
  );

  const dialog = (
    <>
      <ArchiveRunningDialog
        pending={pendingRunning}
        onConfirm={() => {
          const current = pendingRunning;
          setPendingRunning(null);
          current?.confirm();
        }}
        onCancel={() => setPendingRunning(null)}
      />
      <ArchiveWorktreeDialog
        pending={pending}
        onChoose={(deleteWorktrees, remember) => {
          if (remember) writeDeleteWorktreesOnArchive(deleteWorktrees);
          const current = pending;
          setPending(null);
          current?.choose(deleteWorktrees);
        }}
        onCancel={() => setPending(null)}
      />
    </>
  );
  return { requestArchive, dialog };
}

function runningPromptCopy({ runningCount, sessionCount }: PendingRunningPrompt): {
  title: string;
  description: string;
} {
  if (sessionCount === 1) {
    return {
      title: "Archive running session?",
      description:
        "This session's agent is still working. Archiving stops it. You can unarchive the session later.",
    };
  }
  const one = runningCount === 1;
  return {
    title: one ? "Archive a running session?" : `Archive ${runningCount} running sessions?`,
    description: `${runningCount} of the ${sessionCount} sessions you're archiving ${
      one ? "has an agent" : "have agents"
    } still working. Archiving stops ${one ? "it" : "them"}. You can unarchive sessions later.`,
  };
}

function ArchiveRunningDialog({
  pending,
  onConfirm,
  onCancel,
}: {
  pending: PendingRunningPrompt | null;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const open = pending !== null;
  const copy = pending ? runningPromptCopy(pending) : null;
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onCancel();
      }}
    >
      {copy && (
        <DialogContent
          className="sm:max-w-md"
          // Keep dialog clicks off a surrounding sidebar row Link.
          onClick={(e) => e.stopPropagation()}
          data-testid="archive-running-dialog"
        >
          <DialogHeader>
            <DialogTitle>{copy.title}</DialogTitle>
            <DialogDescription>{copy.description}</DialogDescription>
          </DialogHeader>
          <DialogFooter className="border-t-0 bg-transparent">
            <Button
              type="button"
              variant="ghost"
              onClick={onCancel}
              data-testid="archive-running-cancel"
              componentId="archive.running_prompt.cancel"
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={onConfirm}
              data-testid="archive-running-confirm"
              componentId="archive.running_prompt.archive"
            >
              Archive
            </Button>
          </DialogFooter>
        </DialogContent>
      )}
    </Dialog>
  );
}

function ArchiveWorktreeDialog({
  pending,
  onChoose,
  onCancel,
}: {
  pending: PendingPrompt | null;
  onChoose: (deleteWorktrees: boolean, remember: boolean) => void;
  onCancel: () => void;
}) {
  const [remember, setRemember] = useState(false);
  const checkboxId = useId();
  const open = pending !== null;
  const plural = (pending?.worktreeCount ?? 0) > 1;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (next) return;
        setRemember(false);
        onCancel();
      }}
    >
      {open && (
        <DialogContent
          className="sm:max-w-lg"
          // Keep dialog clicks off a surrounding sidebar row Link.
          onClick={(e) => e.stopPropagation()}
          data-testid="archive-worktree-dialog"
        >
          <DialogHeader>
            <DialogTitle>Also delete {plural ? "worktrees" : "the worktree"}?</DialogTitle>
            <DialogDescription>
              {plural
                ? `${pending.worktreeCount} of the sessions you're archiving have a git worktree.`
                : "This session has a git worktree."}{" "}
              Deleting removes the worktree directory, including any uncommitted changes. The branch
              is kept. Worktrees used by other sessions (including archived sessions), or inside
              sandboxes, are kept.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-2">
              <Checkbox
                id={checkboxId}
                checked={remember}
                onCheckedChange={(checked) => setRemember(checked === true)}
                data-testid="archive-worktree-remember"
                componentId="archive.worktree_prompt.remember"
              />
              <label htmlFor={checkboxId} className="cursor-pointer text-ui">
                Remember my choice
              </label>
            </div>
            <p className="pl-6 text-sm text-muted-foreground">
              You can change this any time in Settings › Git.
            </p>
          </div>
          <DialogFooter className="border-t-0 bg-transparent">
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                setRemember(false);
                onChoose(false, remember);
              }}
              data-testid="archive-worktree-keep"
              componentId="archive.worktree_prompt.archive_only"
            >
              No, archive only
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={() => {
                setRemember(false);
                onChoose(true, remember);
              }}
              data-testid="archive-worktree-delete"
              componentId="archive.worktree_prompt.delete_worktrees"
            >
              Yes, delete {plural ? "worktrees" : "worktree"}
            </Button>
          </DialogFooter>
        </DialogContent>
      )}
    </Dialog>
  );
}
