import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AddUserField } from "@/components/PermissionsModal";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { copyText } from "@/lib/clipboard";
import { useRebasePath } from "@/lib/routing";
import { getShareableLink } from "@/lib/shareLink";
import {
  listScheduledTaskRecipients,
  shareScheduledTask,
  unshareScheduledTask,
  type ScheduledTask,
} from "@/lib/scheduledTasksApi";

export function ShareScheduledTaskDialog({
  task,
  onClose,
}: {
  task: Pick<ScheduledTask, "id" | "name">;
  onClose: () => void;
}) {
  const [recipient, setRecipient] = useState("");
  const [copied, setCopied] = useState(false);
  const info = useServerInfo();
  const sharingOff = info === "loading" || info.sharing_mode === "off";
  const queryClient = useQueryClient();
  const key = ["scheduled-task-recipients", task.id];
  const recipients = useQuery({
    queryKey: key,
    queryFn: () => listScheduledTaskRecipients(task.id),
  });
  const change = useMutation({
    mutationFn: ({ userId, remove }: { userId: string; remove: boolean }) =>
      remove ? unshareScheduledTask(task.id, userId) : shareScheduledTask(task.id, userId),
    onSuccess: () => {
      setRecipient("");
      void queryClient.invalidateQueries({ queryKey: key });
    },
  });
  const rebasePath = useRebasePath();
  const link = getShareableLink(`/tasks/shared/${encodeURIComponent(task.id)}`, rebasePath);
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Share {task.name}</DialogTitle>
          <DialogDescription>
            Selected people can view and copy this automation. Runs and sessions stay private.
          </DialogDescription>
        </DialogHeader>
        {recipients.isLoading && <p>Loading people…</p>}
        {recipients.error && <p role="alert">{recipients.error.message}</p>}
        {recipients.data?.map((userId) => (
          <div key={userId} className="flex min-w-0 items-center justify-between gap-2">
            <span className="truncate text-ui">{userId}</span>
            <Button
              variant="ghost"
              size="sm"
              disabled={change.isPending}
              onClick={() => change.mutate({ userId, remove: true })}
              aria-label={`Remove ${userId}`}
            >
              Remove
            </Button>
          </div>
        ))}
        {sharingOff && <p>Sharing is disabled on this server. You can still remove access.</p>}
        <form
          className="flex items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (recipient.trim()) change.mutate({ userId: recipient.trim(), remove: false });
          }}
        >
          <div className="min-w-0 flex-1">
            <label htmlFor="perm-user" className="text-ui">
              Person
            </label>
            <AddUserField
              value={recipient}
              onChange={setRecipient}
              disabled={sharingOff || change.isPending}
            />
          </div>
          <Button type="submit" disabled={sharingOff || change.isPending || !recipient.trim()}>
            Share
          </Button>
        </form>
        {change.error && (
          <p role="alert" className="text-ui text-destructive">
            {change.error.message}
          </p>
        )}
        <Button
          variant="outline"
          onClick={async () => {
            await copyText(link);
            setCopied(true);
          }}
        >
          {copied ? "Link copied" : "Copy link"}
        </Button>
      </DialogContent>
    </Dialog>
  );
}
