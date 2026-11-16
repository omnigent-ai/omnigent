import { lazy, Suspense, type ComponentType } from "react";
import { RenderErrorBoundary } from "@/components/RenderErrorBoundary";
import type { DesignRequestCardProps } from "./DesignRequestMessage";
import { designSubject, NO_SCREENSHOT_NOTE } from "./parseDesignRequest";

/** The card's content without its chunk: the words, and what they were about. */
function PlainCard({ request, words }: DesignRequestCardProps) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      {request.request &&
        (words ?? <p className="whitespace-pre-wrap break-words">{request.request}</p>)}
      <span className="min-w-0 break-words text-muted-foreground text-sm [overflow-wrap:anywhere]">
        {designSubject(request)}
      </span>
      {request.noScreenshot && (
        <span className="text-muted-foreground text-xs">{NO_SCREENSHOT_NOTE}</span>
      )}
    </div>
  );
}

// The card loads in its own chunk, since only design-tool messages need it. Until it has,
// and if it can't, the message shows the same content plainly.
const Card = lazy<ComponentType<DesignRequestCardProps>>(() =>
  import("./DesignRequestMessage")
    .then((module) => ({ default: module.DesignRequestBodyView }))
    .catch(() => ({ default: PlainCard })),
);

/** A design-tool message's body in the chat, as a compact card. */
export function DesignRequestBody(props: DesignRequestCardProps) {
  return (
    <RenderErrorBoundary name="the design request card" fallback={<PlainCard {...props} />}>
      <Suspense fallback={<PlainCard {...props} />}>
        <Card {...props} />
      </Suspense>
    </RenderErrorBoundary>
  );
}
