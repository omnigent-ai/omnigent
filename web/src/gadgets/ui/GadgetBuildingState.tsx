import { type ReactNode, useEffect } from "react";
import { Loader2Icon, PanelsTopLeftIcon, TriangleAlertIcon } from "lucide-react";
import { useQuery } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";

import { getGadget, getGadgetGadgetSessionStatus } from "@/gadgets/api/gadgetApi";
import { GADGETS_KEY } from "./gadgetQueries";

const BUSY_POLL_MS = 3_000;
const IDLE_POLL_MS = 10_000;

// `unknown`: some of its sessions weren't all checked, so it may still be working.
function isBusy(status: string | undefined): boolean {
  return status === "running" || status === "needs_input" || status === "unknown";
}

/** A centered status block in the gadget's place. */
function StateBlock({
  icon,
  title,
  children,
  action,
}: {
  icon: ReactNode;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div
      role="status"
      className="flex min-h-60 flex-1 flex-col items-center justify-center gap-2 px-6 text-center"
    >
      {icon}
      <p className="text-ui font-medium">{title}</p>
      {children && <p className="max-w-sm text-sm text-muted-foreground">{children}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

/**
 * Stands in for a gadget with nothing published while its gadget session builds it. Whether it
 * is building comes from the session's recorded state, not from what the agent says; once a
 * version is published, `onPublished` reloads the gadget and it shows in this place.
 */
export function GadgetBuildingState({
  gadgetId,
  canPublish,
  besideSession,
  onOpenSession,
  onPublished,
}: {
  gadgetId: string;
  /** Whether the viewer can edit the gadget (and so build it). */
  canPublish: boolean;
  /** Whether it is shown beside a session, rather than beside New session. */
  besideSession: boolean;
  /** Set while it fills the screen: shows the session that builds it. */
  onOpenSession?: () => void;
  onPublished: () => void;
}) {
  const session = useQuery({
    queryKey: [...GADGETS_KEY, "session-status", gadgetId],
    queryFn: ({ signal }) => getGadgetGadgetSessionStatus(gadgetId, signal),
    refetchInterval: (query) => (isBusy(query.state.data?.status) ? BUSY_POLL_MS : IDLE_POLL_MS),
  });
  const published = useQuery({
    queryKey: [...GADGETS_KEY, "publish-check", gadgetId],
    queryFn: async ({ signal }) => (await getGadget(gadgetId, signal)).published_revision,
    refetchInterval: isBusy(session.data?.status) ? BUSY_POLL_MS : IDLE_POLL_MS,
  });
  useEffect(() => {
    if (published.data) onPublished();
  }, [published.data, onPublished]);

  const status = session.data?.status;
  if (status === "interrupted") {
    return (
      <StateBlock
        icon={<TriangleAlertIcon className="size-6 text-muted-foreground" />}
        title="Your gadget session stopped"
      >
        It stopped before it published anything. See what happened in the session, then ask it to
        continue.
      </StateBlock>
    );
  }
  if (isBusy(status)) {
    return (
      <StateBlock
        icon={<Loader2Icon className="size-6 animate-spin text-muted-foreground" />}
        title="Building your gadget…"
      >
        {status === "needs_input"
          ? "Your gadget session is waiting for you."
          : status === "unknown"
            ? "Your gadget session may still be working on it; not all of its sessions could be checked. It shows up here once it’s published."
            : "Your gadget session is working on it. It shows up here once it’s published."}
      </StateBlock>
    );
  }
  const openSession = onOpenSession && (
    <Button variant="outline" componentId="gadgets.building.open_session" onClick={onOpenSession}>
      Open session
    </Button>
  );
  return (
    <StateBlock
      icon={<PanelsTopLeftIcon className="size-8 text-muted-foreground/50" />}
      title="Nothing published yet"
      action={canPublish ? openSession : undefined}
    >
      {!canPublish
        ? "The gadget’s owner hasn’t published a version yet."
        : onOpenSession
          ? "Open its session and tell it what to build."
          : besideSession
            ? "Tell this session what it should build."
            : "Describe what it should build in the message beside it, then send it."}
    </StateBlock>
  );
}
