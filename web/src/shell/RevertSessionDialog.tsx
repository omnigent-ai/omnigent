import { useEffect, useState } from "react";
import { HistoryIcon, Loader2Icon } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
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
import { isMessageItem, type MessageItem } from "@/lib/conversationItems";
import { fetchSessionItemsPage, revertSession } from "@/lib/sessionsApi";
import { setSessionTextDraft } from "@/lib/sessionDrafts";
import { isSystemUserContent } from "@/lib/systemMessage";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/chatStore";

function messageText(item: MessageItem): string {
  return item.content
    .filter((block) => block.type === "input_text")
    .map((block) => block.text)
    .join("\n")
    .trim();
}

async function loadUserMessages(sessionId: string): Promise<MessageItem[]> {
  let items: MessageItem[] = [];
  let olderThan: string | undefined;
  while (true) {
    const page = await fetchSessionItemsPage(sessionId, { olderThan, limit: 1000 });
    items = [
      ...page.items.filter(
        (item): item is MessageItem =>
          isMessageItem(item) &&
          item.role === "user" &&
          item.is_meta !== true &&
          !isSystemUserContent(item.content),
      ),
      ...items,
    ];
    if (!page.hasMore || page.items.length === 0) break;
    olderThan = page.items[0].id;
  }
  return items.reverse();
}

export function RevertSessionDialog({
  sourceSessionId,
  sourceWorkspace,
  canModifySource,
  initialUserMessageId,
  open,
  onOpenChange,
}: {
  sourceSessionId: string;
  sourceWorkspace?: string | null;
  canModifySource: boolean;
  initialUserMessageId?: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [revertFiles, setRevertFiles] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const {
    data: messages = [],
    isLoading: loading,
    error: loadError,
  } = useQuery({
    queryKey: ["session", sourceSessionId, "revert-messages"],
    queryFn: () => loadUserMessages(sourceSessionId),
    enabled: open,
  });

  useEffect(() => {
    if (!open || loading) return;
    setSelectedId(
      initialUserMessageId && messages.some((message) => message.id === initialUserMessageId)
        ? initialUserMessageId
        : (messages[0]?.id ?? null),
    );
  }, [initialUserMessageId, loading, messages, open]);

  useEffect(() => {
    if (!open) {
      queryClient.removeQueries({
        queryKey: ["session", sourceSessionId, "revert-messages"],
      });
      setRevertFiles(false);
      setError(null);
    }
  }, [open, queryClient, sourceSessionId]);

  const errorMessage = error ?? loadError?.message ?? null;

  async function handleRevert(): Promise<void> {
    if (!selectedId || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const result = await revertSession(sourceSessionId, selectedId, revertFiles);
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      await useChatStore.getState().reloadCurrent();
      setSessionTextDraft(sourceSessionId, result.draft);
      onOpenChange(false);
      showToast(
        result.fileRevertError ||
          (revertFiles
            ? "Conversation and files reverted."
            : "Conversation reverted; file changes kept."),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Couldn't revert the session.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="grid max-h-[85vh] grid-rows-[auto_minmax(0,1fr)_auto_auto] gap-5 sm:max-w-2xl">
        <DialogHeader className="gap-3 pr-8">
          <DialogTitle className="flex items-center gap-2.5 text-lg leading-tight sm:text-xl">
            <HistoryIcon className="size-5" />
            Revert conversation
          </DialogTitle>
          <DialogDescription className="leading-6">
            Choose a user message to return to. That message and everything after it will be removed
            from this session.
          </DialogDescription>
        </DialogHeader>

        <div
          className="min-h-0 space-y-2 overflow-y-auto rounded-xl border p-2"
          data-testid="revert-message-list"
        >
          {loading ? (
            <div className="flex items-center justify-center gap-2 py-10 text-muted-foreground">
              <Loader2Icon className="size-4 animate-spin" /> Loading messages…
            </div>
          ) : messages.length === 0 ? (
            <p className="p-4 text-center text-muted-foreground">No user messages to revert to.</p>
          ) : (
            messages.map((message) => {
              const text = messageText(message) || "(attachments only)";
              const active = selectedId === message.id;
              return (
                <label
                  key={message.id}
                  className={cn(
                    "flex cursor-pointer gap-3 rounded-lg px-3 py-3 hover:bg-accent",
                    active && "bg-accent ring-1 ring-primary/30",
                  )}
                >
                  <input
                    type="radio"
                    name="revert-message"
                    value={message.id}
                    checked={active}
                    onChange={() => setSelectedId(message.id)}
                    className="mt-1 accent-primary"
                  />
                  <span className="min-w-0">
                    <span className="block text-xs font-medium text-muted-foreground">You</span>
                    <span className="line-clamp-2 block text-sm leading-5">{text}</span>
                  </span>
                </label>
              );
            })
          )}
        </div>

        <div className="rounded-xl border p-4">
          <label
            className={cn(
              "flex items-start gap-3",
              sourceWorkspace && canModifySource ? "cursor-pointer" : "opacity-50",
            )}
          >
            <input
              type="checkbox"
              checked={revertFiles}
              disabled={!sourceWorkspace || !canModifySource}
              onChange={(event) => setRevertFiles(event.target.checked)}
              className="mt-0.5 accent-primary"
            />
            <span>
              <span className="block text-sm font-medium">Revert file changes</span>
              <span className="block text-xs text-muted-foreground">
                Return your files to how they were before this message.
              </span>
            </span>
          </label>
        </div>

        <div>
          {errorMessage && <p className="mb-2 text-sm text-destructive">{errorMessage}</p>}
          <DialogFooter>
            <Button variant="outline" onClick={() => onOpenChange(false)} disabled={submitting}>
              Cancel
            </Button>
            <Button
              onClick={() => void handleRevert()}
              disabled={!selectedId || loading || submitting}
            >
              {submitting && <Loader2Icon className="animate-spin" />}
              Revert
            </Button>
          </DialogFooter>
        </div>
      </DialogContent>
    </Dialog>
  );
}
