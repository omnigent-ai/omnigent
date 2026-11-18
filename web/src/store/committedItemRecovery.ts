import type { CommittedItemRecoveryTrigger } from "@/lib/host";
import { emitOmnigentAnalytics } from "@/lib/analyticsEmit";

/** Report recovered persisted items without transcript content or identity data. */
export function reportRecoveredCommittedItems(
  trigger: CommittedItemRecoveryTrigger,
  count: number,
  secondsSinceLastByte: number | null,
): void {
  if (count <= 0) return;
  emitOmnigentAnalytics({
    type: "committed_items_recovered",
    trigger,
    count,
    secondsSinceLastByte,
  });
  console.warn("Recovered committed items missing from the stream", {
    trigger,
    count,
    secondsSinceLastByte,
  });
}
