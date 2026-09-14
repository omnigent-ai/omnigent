import { getOmnigentServerIdentity } from "./host";
import { getCurrentUserId } from "./identity";

/** Keep draft paths and prompts isolated across servers and signed-in users. */
export function landingStorageKey(prefix: string): string {
  return `${prefix}:${getOmnigentServerIdentity() ?? "default"}:${getCurrentUserId() ?? "anonymous"}`;
}
