// Whether Settings shows the Admin view switch: to an admin-eligible viewer, even with Admin view
// off, wherever the server offers the switch. The page and Settings search both read it.

import { useSyncExternalStore } from "react";

import { isAdminViewEnabled } from "@/lib/adminView";
import { isBootFallbackInfo } from "@/lib/bootCapabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isDatabricksWorkspace } from "@/lib/host";
import { getCurrentAdminEligibility, subscribeIdentity } from "@/lib/identity";

export function useAdminViewSwitchShown(): boolean {
  const eligible = useSyncExternalStore(subscribeIdentity, getCurrentAdminEligibility, () => false);
  const info = useServerInfo();
  // Failed discovery has no capability flag; keep an existing off cookie recoverable.
  return !(
    !eligible ||
    info === "loading" ||
    isBootFallbackInfo(info) ||
    info.admin_view === false ||
    (isAdminViewEnabled() && (!info.admin_view || info.single_user || isDatabricksWorkspace()))
  );
}
