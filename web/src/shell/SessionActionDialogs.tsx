import { useEffect, useState, type FormEvent, type KeyboardEvent } from "react";
import { GitBranchIcon } from "lucide-react";
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
  useRenameConversation,
  useStopAndDeleteConversation,
} from "@/hooks/useConversations";
import { Link, useNavigate } from "@/lib/routing";
import { USER_SESSION_TITLE_MAX_CHARS } from "@/lib/sessionTitles";
import { conversationDisplayLabel } from "./sidebarNav";

interface SessionDialogProps {
  conversation: Conversation;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

function ArchivedToast() {
  return (
    <span>
      View archived sessions in{" "}
      <Link to="/settings/archived" className="font-medium text-primary hover:underline">
        Settings
      </Link>
    </span>
  );
}

export function useArchiveSessionAction() {
  const navigate = useNavigate();
  const archive = useArchiveConversation();

  return (conversation: Conversation, archived: boolean): void => {
    if (!archived) {
      archive.mutate({ id: conversation.id, archived });
      return;
    }
    // The row leaves the sidebar optimistically (useArchiveConversation flips
    // the cached `archived` flag in onMutate), and the caller may be viewing
    // the session being archived, so leave its chat surface now —
    // synchronously — and fire the toast NOW, not in a mutate onSuccess:
    // navigating away unmounts the caller, and per-call mutate callbacks don't
    // fire once their observer unmounts.
    navigate("/", { replace: true });
    archive.mutate({ id: conversation.id, archived: true });
    showToast(<ArchivedToast />);
  };
}

export function RenameSessionDialog({ conversation, open, onOpenChange }: SessionDialogProps) {
  const rename = useRenameConversation();
  const [title, setTitle] = useState(conversation.title ?? "");

  useEffect(() => {
    setTitle(conversation.title ?? "");
  }, [conversation.id, conversation.title, open]);

  const submitRename = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const nextTitle = title.trim();
    if (nextTitle && nextTitle !== (conversation.title ?? "")) {
      rename.mutate({ id: conversation.id, title: nextTitle });
    }
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={submitRename}>
          <DialogHeader>
            <DialogTitle>Rename session</DialogTitle>
            <DialogDescription>Choose a short name that is easy to find later.</DialogDescription>
          </DialogHeader>
          <input
            autoFocus
            aria-label="Session name"
            data-testid="header-rename-conversation-input"
            maxLength={USER_SESSION_TITLE_MAX_CHARS}
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            onFocus={(event) => event.currentTarget.select()}
            onKeyDown={(event: KeyboardEvent<HTMLInputElement>) => {
              if (event.key === "Escape") onOpenChange(false);
            }}
            className="mt-4 h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/50"
          />
          <DialogFooter className="mt-4 border-t-0 bg-transparent">
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={!title.trim() || rename.isPending}>
              Rename
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function DeleteSessionDialog({ conversation, open, onOpenChange }: SessionDialogProps) {
  const navigate = useNavigate();
  const deleteConversation = useStopAndDeleteConversation();
  const [deleteBranch, setDeleteBranch] = useState(false);
  const gitBranch = conversation.git_branch ?? null;
  const label = conversationDisplayLabel(conversation);

  useEffect(() => {
    setDeleteBranch(false);
  }, [conversation.id, open]);

  const confirmDelete = () => {
    onOpenChange(false);
    navigate("/", { replace: true });
    deleteConversation.mutate({
      id: conversation.id,
      deleteBranch: gitBranch !== null && deleteBranch,
    });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete conversation?</DialogTitle>
          <DialogDescription>
            <span className="font-medium break-all">{label}</span> and all of its history will be
            removed. This cannot be undone.
          </DialogDescription>
        </DialogHeader>
        {gitBranch !== null && (
          <div className="flex flex-col gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-3">
            <p className="text-sm text-muted-foreground">
              Optionally clean up the git worktree. These actions are{" "}
              <span className="font-semibold text-destructive">irreversible</span>.
            </p>
            <label className="flex cursor-pointer items-start gap-2 text-ui">
              <input
                type="checkbox"
                data-testid="header-delete-branch-checkbox"
                checked={deleteBranch}
                onChange={(event) => setDeleteBranch(event.target.checked)}
                className="mt-0.5 size-4 shrink-0 accent-destructive"
              />
              <GitBranchIcon className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
              <span className="min-w-0">
                Delete local branch{" "}
                <code className="break-all rounded bg-muted px-1 py-0.5 text-sm">{gitBranch}</code>
              </span>
            </label>
          </div>
        )}
        <DialogFooter className="border-t-0 bg-transparent">
          <Button
            type="button"
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={deleteConversation.isPending}
          >
            Cancel
          </Button>
          <Button
            type="button"
            variant="destructive"
            onClick={confirmDelete}
            disabled={deleteConversation.isPending}
          >
            Delete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
