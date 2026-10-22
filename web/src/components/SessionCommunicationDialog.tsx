import { useState } from "react";
import { ArrowRightIcon, Link2Icon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  useChangeOrchestrationSetting,
  useOrchestrationSetting,
} from "@/hooks/useSessionCommunication";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import type { SessionMessage } from "@/lib/sessionCommunicationApi";

export function SessionMessageDetails({ link }: { link: SessionMessage }) {
  return (
    <div className="space-y-2 rounded-lg border p-3" data-testid="session-link">
      <div className="flex items-center gap-2 text-sm font-medium">
        <span className="min-w-0 flex-1 break-words">{link.source_title || link.source_id}</span>
        <ArrowRightIcon aria-hidden className="size-4 shrink-0 text-brand-accent" />
        <span className="min-w-0 flex-1 break-words">{link.target_title || link.target_id}</span>
      </div>
      <p className="text-xs text-muted-foreground">
        Last message: {new Date(link.last_sent_at * 1000).toLocaleString()}
      </p>
    </div>
  );
}

export function SessionCommunicationDialog({
  title,
  canConnect,
  open,
  onOpenChange,
}: {
  title?: string | null;
  canConnect: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const setting = useOrchestrationSetting(open && canConnect);
  const change = useChangeOrchestrationSetting();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Session communication</DialogTitle>
          <DialogDescription>
            Coordinate {title || "this session"} with your other sessions.
          </DialogDescription>
        </DialogHeader>
        {canConnect && (
          <section
            className="space-y-3 rounded-lg border p-4"
            aria-label="My session communication"
          >
            <h3 className="text-sm font-medium">Allow communication between my sessions</h3>
            <p className="text-sm text-muted-foreground">
              Applies to all sessions you own in this workspace, including future sessions and
              sessions you share. Each request must come from you and you must own both sessions.
            </p>
            <p className="text-sm text-muted-foreground">
              Replies copied into a shared session are visible to everyone who can read it.
              Teammates cannot use your permission to contact your other sessions.
            </p>
            {setting.isLoading && <p role="status">Loading setting…</p>}
            {setting.data && (
              <Button
                variant={setting.data.enabled ? "outline" : "default"}
                disabled={change.isPending}
                onClick={() => setting.data && change.mutate(setting.data)}
              >
                {change.isPending
                  ? "Saving…"
                  : setting.data.enabled
                    ? "Turn off communication"
                    : "Allow communication"}
              </Button>
            )}
            {setting.data && (
              <p role="status" className="text-sm">
                Communication is {setting.data.enabled ? "on" : "off"}.
              </p>
            )}
            {(change.error || setting.error) && (
              <p role="alert" className="text-sm text-destructive">
                {(change.error || setting.error)?.message}
              </p>
            )}
            <p className="text-xs text-muted-foreground">
              Turning this off stops new messages, including replies. Already accepted work
              continues, and copied replies remain readable.
            </p>
          </section>
        )}
        {!canConnect && (
          <p className="text-sm text-muted-foreground">
            Only requests made by the owner can use session communication. Sharing this session does
            not grant access to the owner's other sessions.
          </p>
        )}
        <p className="text-xs text-muted-foreground">
          Canvas arrows show communication activity. They do not grant permission.
        </p>
      </DialogContent>
    </Dialog>
  );
}

export function SessionCommunicationButton({
  sessionId,
  title,
  permissionLevel,
}: {
  sessionId: string;
  title?: string | null;
  permissionLevel?: number | null;
}) {
  const enabled = isFeatureEnabled(useServerInfo(), "session_messaging");
  const [open, setOpen] = useState(false);
  if (!enabled) return null;
  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        aria-label="Session communication"
        title="Session communication"
        onClick={() => setOpen(true)}
      >
        <Link2Icon className="size-4" />
      </Button>
      {open && (
        <SessionCommunicationDialog
          key={sessionId}
          title={title}
          canConnect={permissionLevel === 4}
          open={open}
          onOpenChange={setOpen}
        />
      )}
    </>
  );
}
