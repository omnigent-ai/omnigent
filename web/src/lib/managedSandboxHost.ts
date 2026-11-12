// The id a managed create gives its new sandbox host. The client mints it so the
// create and the session's requests key by that host from the first request.

import { randomUUID } from "./randomUUID";

/** A fresh 32-hex host id for a managed JSON create, fork or move. */
export function managedSandboxHostId(): string {
  return randomUUID().replace(/-/g, "");
}
