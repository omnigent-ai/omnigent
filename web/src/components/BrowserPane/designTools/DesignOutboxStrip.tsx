// The session's design requests on their way or not sent, above the composer like its own
// queued messages. A request belongs to its session, so it stays after its Browser tab closes.
import {
  CircleAlertIcon,
  LoaderCircleIcon,
  MousePointerClickIcon,
  PencilLineIcon,
  RotateCwIcon,
  XIcon,
} from "lucide-react";
import { useShallow } from "zustand/react/shallow";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  forget,
  retryDesignRequest,
  unsettled,
  useDesignOutbox,
  type DesignRequest,
} from "./core/designOutbox";

const ACTION_BUTTON_CLASS =
  "shrink-0 text-muted-foreground transition-colors hover:text-foreground focus-visible:text-foreground max-md:size-11";

function stateText(request: DesignRequest): string {
  if (request.error) return request.error;
  if (request.status === "waiting") return "Waiting for the agent";
  return request.slow ? "The server hasn't answered yet; it's still trying" : "Sending…";
}

function OutboxRow({ request }: { request: DesignRequest }) {
  const stuck = request.status === "failed" || request.status === "denied";
  const ToolIcon = request.tool === "annotate" ? MousePointerClickIcon : PencilLineIcon;
  return (
    <div role="listitem" className="flex min-w-0 items-center gap-1 py-0.5 text-foreground text-sm">
      <ToolIcon className="mx-1 size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
      <div className="flex min-w-0 flex-1 items-center gap-2 max-md:flex-col max-md:items-start max-md:gap-0.5">
        <span className="min-w-0 max-w-full truncate" title={request.label}>
          {request.label}
        </span>
        {/* A failure's text wraps, so what it says to check before retrying can be read. */}
        <span
          className={cn(
            "flex min-w-0 shrink items-center gap-1 text-muted-foreground text-sm",
            stuck ? "items-start text-destructive" : "truncate",
          )}
          title={stateText(request)}
        >
          {stuck ? (
            <CircleAlertIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
          ) : (
            request.status === "sending" && (
              <LoaderCircleIcon
                className="size-3.5 shrink-0 motion-safe:animate-spin"
                aria-hidden="true"
              />
            )
          )}
          <span className={stuck ? "min-w-0 break-words" : "truncate"}>{stateText(request)}</span>
        </span>
      </div>
      {request.status === "failed" && (
        <Button
          componentId="browserDesignTools.outbox.retry"
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label={`Retry ${request.label}`}
          title="Retry"
          className={ACTION_BUTTON_CLASS}
          onClick={() => retryDesignRequest(request.id)}
        >
          <RotateCwIcon className="size-3.5" aria-hidden="true" />
        </Button>
      )}
      {/* Not while it's sending: it may be reaching the agent right now. */}
      {request.status !== "sending" && (
        <Button
          componentId="browserDesignTools.outbox.remove"
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label={`Remove ${request.label}`}
          title="Remove"
          className={ACTION_BUTTON_CLASS}
          onClick={() => forget(request.id)}
        >
          <XIcon className="size-4" aria-hidden="true" />
        </Button>
      )}
    </div>
  );
}

export interface DesignOutboxStripProps {
  conversationId: string;
  widthClassName?: string;
}

/** The session's design requests that aren't sent yet, with Retry and Remove. */
export function DesignOutboxStrip({ conversationId, widthClassName }: DesignOutboxStripProps) {
  const requests = useDesignOutbox(
    useShallow((state) =>
      state.requests.filter(
        (request) => request.conversationId === conversationId && unsettled(request),
      ),
    ),
  );
  if (requests.length === 0) return null;
  return (
    <div
      data-testid="design-outbox-strip"
      className={cn(
        "composer-queued-surface mx-auto -mb-4 flex w-full flex-col rounded-t-2xl px-2 pt-1.5 pb-5.5 md:px-3",
        widthClassName,
      )}
    >
      <div
        role="list"
        aria-label="Design requests"
        className="flex max-h-32 flex-col gap-1 overflow-y-auto overscroll-contain"
      >
        {requests.map((request) => (
          <OutboxRow key={request.id} request={request} />
        ))}
      </div>
    </div>
  );
}
