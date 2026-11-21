// A design-tool message's content in the chat: the request, its subject and the no-screenshot
// note. Both the card (DesignRequestMessage.tsx, its own chunk) and its plain fallback show it.
import type { ReactNode } from "react";
import { designSubject, NO_SCREENSHOT_NOTE, type DesignRequest } from "./parseDesignRequest";

export interface DesignRequestCardProps {
  request: DesignRequest;
  /** The request's words as the chat renders them (Markdown); plain text without. */
  words?: ReactNode;
}

/** The request's words, then what it was about (after `icon`, the card's), then any note. */
export function DesignRequestContent({
  request,
  words,
  icon,
}: DesignRequestCardProps & { icon?: ReactNode }) {
  return (
    <>
      {request.request &&
        (words ?? <p className="whitespace-pre-wrap break-words">{request.request}</p>)}
      <span className="flex min-w-0 items-start gap-1 text-muted-foreground text-sm">
        {icon}
        {/* Wraps rather than clips: its fields are capped, and long selectors need reading. */}
        <span className="min-w-0 break-words [overflow-wrap:anywhere]">
          {designSubject(request)}
        </span>
      </span>
      {request.noScreenshot && (
        <span className="text-muted-foreground text-xs">{NO_SCREENSHOT_NOTE}</span>
      )}
    </>
  );
}
