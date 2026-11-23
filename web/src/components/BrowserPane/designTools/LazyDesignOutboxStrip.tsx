import { Suspense } from "react";
import { RenderErrorBoundary } from "@/components/RenderErrorBoundary";
import type { DesignOutboxStripProps } from "./DesignOutboxStrip";
import { useHasDesignRequests } from "./core/designOutboxPresence";
import { retryingChunk } from "./retryingChunk";

// Loads with the design tools' outbox, only once the session has requests to show; a chunk
// that can't load leaves the composer as it was, and is tried again later.
const strip = retryingChunk<DesignOutboxStripProps>(() =>
  import("./DesignOutboxStrip").then((module) => module.DesignOutboxStrip),
);

type StripProps = Omit<DesignOutboxStripProps, "conversationId"> & {
  conversationId: string | null;
};

function LazyDesignOutboxStrip({ conversationId, ...rest }: StripProps) {
  const shown = useHasDesignRequests(conversationId);
  const Strip = strip.useComponent();
  if (!shown || conversationId === null) return null;
  return (
    <Suspense fallback={null}>
      <Strip conversationId={conversationId} {...rest} />
    </Suspense>
  );
}

/** The session's design requests above the composer; a failure leaves the composer as it was. */
export function DesignOutboxStrip(props: StripProps) {
  // A render failure hides the strip until the conversation changes.
  return (
    <RenderErrorBoundary
      name="the design requests"
      fallback={null}
      resetKey={props.conversationId ?? undefined}
    >
      <LazyDesignOutboxStrip {...props} />
    </RenderErrorBoundary>
  );
}
