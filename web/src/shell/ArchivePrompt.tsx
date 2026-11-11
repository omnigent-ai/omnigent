import { FolderIcon, GitBranchIcon, ServerIcon } from "lucide-react";
import { useCallback, useId, useRef, useState, type ReactNode } from "react";

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
import { useHosts } from "@/hooks/useHosts";
import { isSessionWorking } from "@/hooks/useSessionState";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  readDeleteWorktreesOnArchive,
  writeDeleteWorktreesOnArchive,
} from "@/lib/archiveWorktreePreferences";
import { recordedWorktreeRoot, WORKTREE_ROOT_LABEL_KEY } from "@/lib/worktreeRoot";

import { basename } from "./WorkspacePicker";

/** Called with the ids whose worktree should be removed (empty = archive only). */
type ProceedFn = (deleteWorktreeIds: ReadonlySet<string>) => void;

/** One worktree the archive prompt names; several sessions may share it. */
export interface ArchiveWorktreeTarget {
  /** Host holding the worktree, e.g. `"host_a1b2"`; null when not host-bound. */
  hostId: string | null;
  /** Worktree root directory, e.g. `"/repo/.worktrees/login"`; null when unknown. */
  path: string | null;
  /** Branch checked out in the worktree, e.g. `"feature/login"`. */
  branch: string;
}

interface PendingPrompt {
  worktreeCount: number;
  worktrees: readonly ArchiveWorktreeTarget[];
  choose: (deleteWorktrees: boolean) => void;
}

/** Directory archive cleanup would remove for the session, or null when unknown. */
function sessionWorktreeRoot(conversation: Conversation): Promise<string | null> {
  const workspace = conversation.workspace;
  if (!workspace?.trim()) return Promise.resolve(null);
  return recordedWorktreeRoot(workspace, conversation.labels?.[WORKTREE_ROOT_LABEL_KEY]);
}

function worktreeTargetKey({ hostId, path, branch }: ArchiveWorktreeTarget): string {
  return JSON.stringify(path === null ? [hostId, "branch", branch] : [hostId, "path", path]);
}

/** Distinct worktrees of the sessions, in order; sessions without a root group by branch. */
export async function archiveWorktreeTargets(
  conversations: readonly Conversation[],
): Promise<ArchiveWorktreeTarget[]> {
  const resolved = await Promise.all(
    conversations.flatMap((conversation) => {
      const branch = conversation.git_branch;
      if (branch == null) return [];
      const hostId = conversation.host_id ?? null;
      return [sessionWorktreeRoot(conversation).then((path) => ({ hostId, path, branch }))];
    }),
  );
  const targets = new Map<string, ArchiveWorktreeTarget>();
  for (const target of resolved) {
    const key = worktreeTargetKey(target);
    if (!targets.has(key)) targets.set(key, target);
  }
  // Git checks a branch out in one worktree at a time, so a rootless entry
  // whose branch has a known root on the same host names that worktree.
  const values = [...targets.values()];
  const branchesWithRoot = new Set(
    values
      .filter((target) => target.path !== null)
      .map((target) => worktreeTargetKey({ ...target, path: null })),
  );
  return values.filter(
    (target) => target.path !== null || !branchesWithRoot.has(worktreeTargetKey(target)),
  );
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
  const latestWorktreeRequest = useRef(0);
  const [pendingRunning, setPendingRunning] = useState<PendingRunningPrompt | null>(null);
  const serverInfo = useServerInfo();
  const cleanupSupported = serverInfo !== "loading" && serverInfo.archive_worktree_cleanup === true;

  const requestWorktreeChoice = useCallback(
    (conversations: readonly Conversation[], proceed: ProceedFn) => {
      const worktreeSessions = conversations.filter((c) => c.git_branch != null);
      const worktreeIds = new Set(worktreeSessions.map((c) => c.id));
      if (!cleanupSupported || worktreeIds.size === 0) {
        proceed(new Set());
        return;
      }
      const preference = readDeleteWorktreesOnArchive();
      if (preference !== null) {
        proceed(preference ? worktreeIds : new Set());
        return;
      }
      const request = ++latestWorktreeRequest.current;
      void archiveWorktreeTargets(worktreeSessions).then((worktrees) => {
        if (request !== latestWorktreeRequest.current) return;
        setPending({
          worktreeCount: worktreeIds.size,
          worktrees,
          choose: (deleteWorktrees) => proceed(deleteWorktrees ? worktreeIds : new Set()),
        });
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
          <ArchiveWorktreeList worktrees={pending.worktrees} />
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

export function ArchiveWorktreeList({
  worktrees,
}: {
  worktrees: readonly ArchiveWorktreeTarget[];
}) {
  const spansHosts = new Set(worktrees.map((target) => target.hostId)).size > 1;
  return (
    <ul
      aria-label={worktrees.length === 1 ? "Worktree" : "Worktrees"}
      className="max-h-48 divide-y divide-border overflow-y-auto rounded-md border border-border"
      data-testid="archive-worktree-list"
    >
      {worktrees.map((target) => (
        <li
          key={worktreeTargetKey(target)}
          className="flex min-w-0 flex-col gap-0.5 px-3 py-2"
          data-testid="archive-worktree-item"
        >
          {target.path !== null && (
            <>
              <span className="flex min-w-0 items-center gap-1.5 text-ui font-medium">
                <FolderIcon aria-hidden className="size-3.5 shrink-0" />
                <span className="truncate" data-testid="archive-worktree-name">
                  {basename(target.path)}
                </span>
              </span>
              <span className="break-all pl-5 font-mono text-xs text-muted-foreground">
                {target.path}
              </span>
            </>
          )}
          <span className="flex min-w-0 items-center gap-1.5 font-mono text-xs text-muted-foreground">
            <GitBranchIcon aria-hidden className="size-3.5 shrink-0" />
            <span className="break-all">{target.branch}</span>
          </span>
          {spansHosts && <ArchiveWorktreeHost hostId={target.hostId} />}
        </li>
      ))}
    </ul>
  );
}

/** Host line, shown only when one archive spans worktrees on several hosts. */
function ArchiveWorktreeHost({ hostId }: { hostId: string | null }) {
  const { data: hosts } = useHosts({ includeSandbox: true });
  const label =
    hostId === null ? "No host" : (hosts?.find((host) => host.host_id === hostId)?.name ?? hostId);
  return (
    <span className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
      <ServerIcon aria-hidden className="size-3.5 shrink-0" />
      <span className="truncate" data-testid="archive-worktree-host">
        {label}
      </span>
    </span>
  );
}
