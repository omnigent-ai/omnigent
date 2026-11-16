// A design-tool message's body in the chat, as a compact card: what was asked and what it
// was about. It renders inside the ordinary user bubble; desktop design mode sends the same
// format, so its messages get the card too.
import { MousePointerClickIcon, PencilLineIcon, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { designSubject, NO_SCREENSHOT_NOTE, type DesignRequest } from "./parseDesignRequest";

const KIND_ICONS: Record<DesignRequest["kind"], LucideIcon> = {
  element: MousePointerClickIcon,
  drawing: PencilLineIcon,
};

export interface DesignRequestCardProps {
  request: DesignRequest;
  /** The request's words as the chat renders them (Markdown); plain text without. */
  words?: ReactNode;
}

/** A design-tool message's text, compact: the request and its subject. */
export function DesignRequestBodyView({ request, words }: DesignRequestCardProps) {
  const Icon = KIND_ICONS[request.kind];
  return (
    <div data-testid="design-request" className="flex min-w-0 flex-col gap-1">
      {request.request &&
        (words ?? <p className="whitespace-pre-wrap break-words">{request.request}</p>)}
      <span className="flex min-w-0 items-start gap-1 text-muted-foreground text-sm">
        <Icon className="mt-0.5 size-3.5 shrink-0" />
        {/* Wraps rather than clips: its fields are capped, and long selectors need reading. */}
        <span className="min-w-0 break-words [overflow-wrap:anywhere]">
          {designSubject(request)}
        </span>
      </span>
      {request.noScreenshot && (
        <span className="text-muted-foreground text-xs">{NO_SCREENSHOT_NOTE}</span>
      )}
    </div>
  );
}
