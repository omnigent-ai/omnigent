// Which server this device's kept data was read through, as part of every device-local key, so
// one server's answers and choices never show under another. Its own module, so stores the read
// path consults can scope by server without importing the read path.

import { getApiBasePath } from "@/lib/basePath";
import { getOmnigentServerIdentity } from "@/lib/host";

/** The server's stable identity and base path, or null where it has no stable identity. */
export function durableServerScope(): string | null {
  const identity = getOmnigentServerIdentity();
  return identity === null ? null : JSON.stringify([identity, getApiBasePath()]);
}
