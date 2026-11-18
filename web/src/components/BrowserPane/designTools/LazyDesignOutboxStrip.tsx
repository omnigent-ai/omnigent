import { lazy, Suspense, useCallback, useEffect, useState, type ComponentType } from "react";
import { RenderErrorBoundary } from "@/components/RenderErrorBoundary";
import type { DesignOutboxStripProps } from "./DesignOutboxStrip";
import { useHasDesignRequests } from "./core/designOutboxPresence";

/** After the strip's chunk first fails to load, how long before the strip tries it again. */
export const STRIP_RETRY_MS = 5_000;
/** The longest wait between tries: each failed one doubles it, and a load resets it. */
export const STRIP_RETRY_MAX_MS = 5 * 60_000;
let retryMs = STRIP_RETRY_MS;

type LoadedProps = DesignOutboxStripProps & { onUnavailable: () => void };

/** In place of a strip whose chunk can't load: nothing, and after `waitMs`, another try. */
function unavailable(waitMs: number): ComponentType<LoadedProps> {
  return function Unavailable({ onUnavailable }: LoadedProps) {
    useEffect(() => {
      const timer = setTimeout(onUnavailable, waitMs);
      return () => clearTimeout(timer);
    }, [onUnavailable]);
    return null;
  };
}

// Loads with the design tools' outbox, and only once the session has requests to show. A
// chunk that can't load leaves the composer as it was, and isn't kept: the strip tries it
// again after a while (the boundary below hands such failures on to the app's).
function loadStrip() {
  return lazy<ComponentType<LoadedProps>>(() =>
    import("./DesignOutboxStrip")
      .then((module) => {
        const loaded = module.DesignOutboxStrip;
        retryMs = STRIP_RETRY_MS;
        return { default: loaded };
      })
      .catch(() => {
        const waitMs = retryMs;
        retryMs = Math.min(retryMs * 2, STRIP_RETRY_MAX_MS);
        return { default: unavailable(waitMs) };
      }),
  );
}
let Strip = loadStrip();

type StripProps = Omit<DesignOutboxStripProps, "conversationId"> & {
  conversationId: string | null;
};

function LazyDesignOutboxStrip({ conversationId, ...rest }: StripProps) {
  const shown = useHasDesignRequests(conversationId);
  const [, setAttempts] = useState(0);
  const Loaded = Strip;
  const retry = useCallback(() => {
    // Another strip may have started the next try already.
    if (Strip === Loaded) Strip = loadStrip();
    setAttempts((attempts) => attempts + 1);
  }, [Loaded]);
  if (!shown || conversationId === null) return null;
  return (
    <Suspense fallback={null}>
      <Loaded conversationId={conversationId} {...rest} onUnavailable={retry} />
    </Suspense>
  );
}

/** The session's design requests above the composer; a failure leaves the composer as it was. */
export function DesignOutboxStrip(props: StripProps) {
  return (
    <RenderErrorBoundary name="the design requests" fallback={null}>
      <LazyDesignOutboxStrip {...props} />
    </RenderErrorBoundary>
  );
}
