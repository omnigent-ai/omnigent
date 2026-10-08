import { Loader2Icon, PaperclipIcon, PencilIcon, Trash2Icon, XIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { Message, MessageContent } from "@/components/ai-elements/message";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { attachmentKey } from "@/lib/attachments";
import { cn } from "@/lib/utils";
import { type FailedUserMessage, useChatStore } from "@/store/chatStore";

function RetainedAttachment({ file, onRemove }: { file: File; onRemove?: () => void }) {
  const [preview, setPreview] = useState<string | null>(null);
  useEffect(() => {
    if (!file.type.startsWith("image/") || typeof URL.createObjectURL !== "function") {
      setPreview(null);
      return;
    }
    const url = URL.createObjectURL(file);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  return (
    <div className="flex max-w-full flex-col gap-1.5">
      {preview && (
        <img
          src={preview}
          alt={file.name}
          className="max-h-48 max-w-full rounded-lg object-contain"
        />
      )}
      <span className="flex w-fit max-w-full items-center gap-1.5 text-xs text-muted-foreground">
        <PaperclipIcon className="size-3 shrink-0" aria-hidden="true" />
        <span className="truncate">{file.name}</span>
        {onRemove && (
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label={`Remove ${file.name}`}
            onClick={onRemove}
          >
            <XIcon className="size-3" aria-hidden="true" />
          </Button>
        )}
      </span>
    </div>
  );
}

function FooterSeparator() {
  return (
    <span aria-hidden="true" className="text-muted-foreground/50">
      ·
    </span>
  );
}

interface FailedSendMessageProps {
  message: FailedUserMessage;
  onRetry: () => void;
  onCheck: () => Promise<void>;
  onEdit: (text: string, files: File[]) => boolean;
  onDiscard: () => void;
}

/**
 * A send the server is not known to have taken, kept in the transcript with a
 * compact footer: "Failed to send · Retry" (editable, attachments removable), or
 * "Send unconfirmed · Check" while a resend could still duplicate it.
 */
export function FailedSendMessage({
  message,
  onRetry,
  onCheck,
  onEdit,
  onDiscard,
}: FailedSendMessageProps) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(message.text);
  const [files, setFiles] = useState(message.files);
  const [checking, setChecking] = useState(false);
  const unconfirmed = message.unsettled === true;
  const shownFiles = editing ? files : message.files;
  const editId = `edit-failed-send-${message.stableId}`;
  const editEmpty = text.trim() === "" && files.length === 0;

  const startEditing = () => {
    setText(message.text);
    setFiles(message.files);
    setEditing(true);
  };
  const check = async () => {
    setChecking(true);
    try {
      await onCheck();
    } catch {
      // The card stays unconfirmed; the user can Check again.
    } finally {
      setChecking(false);
    }
  };

  return (
    <Message
      from="user"
      className="max-w-[640px]"
      data-testid="failed-send-message"
      data-delivery-status={unconfirmed ? "unconfirmed" : "not_sent"}
    >
      <div className="ml-auto flex max-w-full flex-col items-end gap-0.5">
        <MessageContent className={cn(editing && "w-full min-w-[min(28rem,100%)]")}>
          {shownFiles.length > 0 && (
            <div className="flex flex-wrap gap-2 pb-1">
              {shownFiles.map((file) => (
                <RetainedAttachment
                  key={attachmentKey(file)}
                  file={file}
                  onRemove={
                    editing
                      ? () => setFiles((current) => current.filter((f) => f !== file))
                      : undefined
                  }
                />
              ))}
            </div>
          )}
          {editing ? (
            <form
              className="flex w-full min-w-0 flex-col gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                if (editEmpty || unconfirmed) return;
                // Keep the editor open if the store refused the edit (a retry
                // owns the message), so the change is not silently dropped.
                if (onEdit(text, files)) setEditing(false);
              }}
            >
              <label className="sr-only" htmlFor={editId}>
                Edit unsent message
              </label>
              <Textarea
                autoFocus
                id={editId}
                value={text}
                onChange={(event) => setText(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Escape") setEditing(false);
                }}
                rows={3}
                className="min-h-24 resize-y border-0 bg-transparent p-0 text-ui shadow-none dark:bg-transparent"
              />
              <div className="flex justify-end gap-4">
                <Button
                  type="button"
                  size="xs"
                  variant="link"
                  className="px-0 text-xs text-muted-foreground"
                  onClick={() => setEditing(false)}
                >
                  Cancel
                </Button>
                <Button
                  type="submit"
                  size="xs"
                  variant="link"
                  className="px-0 text-xs text-foreground"
                  disabled={editEmpty || unconfirmed}
                >
                  Save changes
                </Button>
              </div>
            </form>
          ) : (
            message.text !== "" && <p className="whitespace-pre-wrap break-words">{message.text}</p>
          )}
        </MessageContent>
        <div className="flex min-h-6 max-w-full flex-wrap items-center gap-x-1.5 px-1 text-xs">
          <span
            role="status"
            className={unconfirmed ? "text-muted-foreground" : "text-destructive"}
          >
            {unconfirmed ? "Send unconfirmed" : "Failed to send"}
          </span>
          {message.reason !== "" && (
            <>
              <FooterSeparator />
              <span className="min-w-0 break-words text-muted-foreground">{message.reason}</span>
            </>
          )}
          {!editing && (
            <>
              <FooterSeparator />
              {unconfirmed ? (
                <Button
                  type="button"
                  variant="link"
                  size="xs"
                  className="px-0 text-xs text-foreground"
                  disabled={checking}
                  onClick={() => void check()}
                >
                  {checking && (
                    <Loader2Icon
                      className="size-3 animate-spin motion-reduce:animate-none"
                      aria-hidden="true"
                    />
                  )}
                  {checking ? "Checking…" : "Check"}
                </Button>
              ) : (
                <>
                  <Button
                    type="button"
                    variant="link"
                    size="xs"
                    className="px-0 text-xs text-foreground"
                    onClick={onRetry}
                  >
                    Retry
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-xs"
                    aria-label="Edit"
                    onClick={startEditing}
                  >
                    <PencilIcon className="size-3.5" aria-hidden="true" />
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-xs"
                    aria-label="Discard"
                    onClick={onDiscard}
                  >
                    <Trash2Icon className="size-3.5" aria-hidden="true" />
                  </Button>
                </>
              )}
            </>
          )}
        </div>
      </div>
    </Message>
  );
}

/** The conversation's retained failed sends, oldest first, wired to the store. */
export function FailedSendMessages({ messages }: { messages: FailedUserMessage[] }) {
  if (messages.length === 0) return null;
  const actions = () => useChatStore.getState();
  return messages.map((message) => (
    <FailedSendMessage
      key={message.stableId}
      message={message}
      onRetry={() => void actions().retryFailedMessage(message.stableId)}
      onCheck={() => actions().checkFailedMessage(message.stableId)}
      onEdit={(text, files) => actions().editFailedMessage(message.stableId, text, files)}
      onDiscard={() => actions().discardFailedMessage(message.stableId)}
    />
  ));
}
