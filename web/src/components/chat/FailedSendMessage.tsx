import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Loader2Icon, PaperclipIcon, PencilIcon, XIcon } from "lucide-react";
import {
  Message,
  MessageAction,
  MessageActions,
  MessageContent,
} from "@/components/ai-elements/message";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { attachmentKey } from "@/lib/attachments";
import { cn } from "@/lib/utils";
import { type FailedUserMessage, useChatStore } from "@/store/chatStore";

function subscribeOnline(onChange: () => void) {
  window.addEventListener("online", onChange);
  window.addEventListener("offline", onChange);
  return () => {
    window.removeEventListener("online", onChange);
    window.removeEventListener("offline", onChange);
  };
}

export function useBrowserOnline(): boolean {
  return useSyncExternalStore(
    subscribeOnline,
    () => navigator.onLine,
    () => true,
  );
}

function SavedAttachment({ file, onRemove }: { file: File; onRemove?: () => void }) {
  const [preview, setPreview] = useState<string | null>(null);
  useEffect(() => {
    if (!file.type.startsWith("image/")) return;
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

interface FailedSendMessageProps {
  message: FailedUserMessage;
  online: boolean;
  onRetry: () => void;
  onCheck: () => void;
  onEdit: (text: string, files: File[]) => void;
}

export function FailedSendMessage({
  message,
  online,
  onRetry,
  onCheck,
  onEdit,
}: FailedSendMessageProps) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(message.text);
  const [files, setFiles] = useState(message.files);
  const messageElement = useRef<HTMLDivElement>(null);
  const wasEditing = useRef(false);
  const notSent = message.status === "not_sent";
  const checking = message.status === "checking";
  const isEditing = editing && notSent;
  const visibleFiles = isEditing ? files : message.files;
  useEffect(() => {
    if (!notSent) setEditing(false);
    if (wasEditing.current && !isEditing && notSent) {
      messageElement.current
        ?.querySelector<HTMLButtonElement>("[data-edit-failed-message]")
        ?.focus();
    }
    wasEditing.current = isEditing;
  }, [isEditing, notSent]);
  const title = notSent
    ? "Failed to send"
    : checking
      ? "Checking send status…"
      : "Send unconfirmed";

  return (
    <Message
      from="user"
      className="max-w-[640px]"
      data-testid="failed-send-message"
      data-delivery-status={message.status}
    >
      <div ref={messageElement} className="ml-auto flex max-w-full flex-col items-end gap-0.5">
        <MessageContent className={cn(isEditing && "w-full min-w-[min(28rem,100%)]")}>
          {visibleFiles.length > 0 && (
            <div className="flex flex-wrap gap-2 pb-1">
              {visibleFiles.map((file, index) => (
                <SavedAttachment
                  key={attachmentKey(file)}
                  file={file}
                  onRemove={
                    isEditing
                      ? () => {
                          setFiles((current) => current.filter((_, i) => i !== index));
                          messageElement.current?.querySelector("textarea")?.focus();
                        }
                      : undefined
                  }
                />
              ))}
            </div>
          )}
          {isEditing ? (
            <form
              className="flex w-full min-w-0 flex-col gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                if (!notSent || (!text.trim() && files.length === 0)) return;
                onEdit(text, files);
                setEditing(false);
              }}
            >
              <label className="sr-only" htmlFor={`edit-${message.stableId}`}>
                Edit unsent message
              </label>
              <Textarea
                autoFocus
                id={`edit-${message.stableId}`}
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
                  disabled={!text.trim() && files.length === 0}
                >
                  Save changes
                </Button>
              </div>
            </form>
          ) : (
            message.text && <p className="whitespace-pre-wrap break-words">{message.text}</p>
          )}
        </MessageContent>
        <div className="flex min-h-6 max-w-full items-center gap-1.5 px-1 text-xs">
          {notSent && !isEditing && (
            <MessageActions className="opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100 [@media(hover:none)]:opacity-100">
              <MessageAction
                tooltip="Edit"
                size="icon-xs"
                data-edit-failed-message=""
                onClick={() => {
                  setText(message.text);
                  setFiles(message.files);
                  setEditing(true);
                }}
              >
                <PencilIcon className="size-3.5" aria-hidden="true" />
              </MessageAction>
            </MessageActions>
          )}
          <span role="status" className="flex items-center gap-1.5">
            {checking ? (
              <Loader2Icon
                className="size-3 shrink-0 animate-spin text-muted-foreground motion-reduce:animate-none"
                aria-hidden="true"
              />
            ) : null}
            <span
              className={notSent ? "text-destructive" : "text-muted-foreground"}
              title={message.message}
            >
              {title}
            </span>
            {!online && !checking && (
              <>
                <span aria-hidden="true" className="text-muted-foreground/50">
                  ·
                </span>
                <span className="text-muted-foreground">Offline</span>
              </>
            )}
          </span>
          {online && !checking && !isEditing && (
            <>
              <span aria-hidden="true" className="text-muted-foreground/50">
                ·
              </span>
              <Button
                type="button"
                variant="link"
                size="xs"
                className="px-0 text-xs text-foreground"
                onClick={notSent ? onRetry : onCheck}
              >
                {notSent ? "Retry" : "Check"}
              </Button>
            </>
          )}
        </div>
      </div>
    </Message>
  );
}

export function FailedSendMessages({ messages }: { messages: FailedUserMessage[] }) {
  const online = useBrowserOnline();
  const checked = useRef(new Set<string>());
  useEffect(() => {
    if (!online) {
      checked.current.clear();
      return;
    }
    for (const message of messages) {
      if (message.status !== "unknown" || checked.current.has(message.stableId)) continue;
      checked.current.add(message.stableId);
      void useChatStore.getState().checkFailedMessage(message.stableId);
    }
  }, [messages, online]);

  return messages.map((message) => (
    <FailedSendMessage
      key={message.stableId}
      message={message}
      online={online}
      onRetry={() => void useChatStore.getState().retryFailedMessage(message.stableId)}
      onCheck={() => void useChatStore.getState().checkFailedMessage(message.stableId)}
      onEdit={(text, files) =>
        useChatStore.getState().updateFailedMessage(message.stableId, text, files)
      }
    />
  ));
}
