import { useState } from "react";

/**
 * The first non-null `value` seen, kept until `resetKey` changes, so a choice made once
 * from live data (such as which lane to open) doesn't move as that data changes.
 */
export function usePinnedValue<T>(value: T | null, resetKey: string | null): T | null {
  const [pinned, setPinned] = useState<{ key: string | null; value: T | null }>({
    key: resetKey,
    value,
  });
  if (pinned.key !== resetKey) {
    setPinned({ key: resetKey, value });
    return value;
  }
  if (pinned.value === null && value !== null) {
    setPinned({ key: resetKey, value });
    return value;
  }
  return pinned.value;
}
