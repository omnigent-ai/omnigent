// A design-tool message's content in the chat: the request (or Live Edit's changes), its
// subject and the no-screenshot note. The card (DesignRequestMessage.tsx) and its fallback show it.
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
      {request.kind !== "edits" &&
        request.request &&
        (words ?? <p className="whitespace-pre-wrap break-words">{request.request}</p>)}
      {request.edits && request.edits.length > 0 && (
        <ul className="flex flex-col gap-2">
          {request.edits.map((edit, index) => (
            // oxlint-disable-next-line react/no-array-index-key -- a fixed list from one message
            <li key={index} className="flex min-w-0 flex-col break-words">
              <span className="truncate font-mono text-muted-foreground text-sm">
                {edit.element}
              </span>
              <span className="whitespace-pre-wrap text-muted-foreground text-sm line-through">
                {edit.before}
              </span>
              <span className="whitespace-pre-wrap">{edit.after}</span>
            </li>
          ))}
        </ul>
      )}
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
