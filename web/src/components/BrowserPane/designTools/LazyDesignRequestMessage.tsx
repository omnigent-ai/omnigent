import { Suspense } from "react";
import { RenderErrorBoundary } from "@/components/RenderErrorBoundary";
import { DesignRequestContent, type DesignRequestCardProps } from "./DesignRequestContent";
import { retryingChunk } from "./retryingChunk";

/** The card's content without its chunk: the words, and what they were about. */
function PlainCard(props: DesignRequestCardProps) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <DesignRequestContent {...props} />
    </div>
  );
}

// The card loads in its own chunk, since only design-tool messages need it. Until it has,
// and while it can't, the message shows the same content plainly.
const card = retryingChunk<DesignRequestCardProps>(
  () => import("./DesignRequestMessage").then((module) => module.DesignRequestBodyView),
  PlainCard,
);

/** A design-tool message's body in the chat, as a compact card. */
export function DesignRequestBody(props: DesignRequestCardProps) {
  const Card = card.useComponent();
  return (
    <RenderErrorBoundary name="the design request card" fallback={<PlainCard {...props} />}>
      <Suspense fallback={<PlainCard {...props} />}>
        <Card {...props} />
      </Suspense>
    </RenderErrorBoundary>
  );
}
