import { useEffect, useState, type FormEvent, type KeyboardEvent } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { type Conversation, useRenameConversation } from "@/hooks/useConversations";
import { USER_SESSION_TITLE_MAX_CHARS } from "@/lib/sessionTitles";

/** Renames a session from a dialog, for surfaces without an inline title editor. */
export function RenameSessionDialog({
  conversation,
  open,
  onOpenChange,
  inputTestId = "rename-session-input",
}: {
  conversation: Conversation;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  inputTestId?: string;
}) {
  const rename = useRenameConversation();
  const [title, setTitle] = useState(conversation.title ?? "");

  useEffect(() => {
    if (!open) setTitle(conversation.title ?? "");
  }, [conversation.title, open]);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmed = title.trim();
    if (trimmed && trimmed !== (conversation.title ?? "")) {
      rename.mutate({ id: conversation.id, title: trimmed });
    }
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={submit}>
          <DialogHeader>
            <DialogTitle>Rename session</DialogTitle>
            <DialogDescription>Choose a short name that is easy to find later.</DialogDescription>
          </DialogHeader>
          <input
            autoFocus
            aria-label="Session name"
            data-testid={inputTestId}
            maxLength={USER_SESSION_TITLE_MAX_CHARS}
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            onKeyDown={(event: KeyboardEvent<HTMLInputElement>) => {
              if (event.key === "Escape") onOpenChange(false);
            }}
            className="mt-4 h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/50"
          />
          <DialogFooter className="mt-4 border-t-0 bg-transparent mobile-app:flex-col-reverse mobile-app:gap-1">
            <Button
              type="button"
              variant="ghost"
              className="mobile-touch-target mobile-app:w-full"
              onClick={() => onOpenChange(false)}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              className="mobile-touch-target mobile-app:w-full"
              disabled={!title.trim() || rename.isPending}
            >
              Rename
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
