import { useEffect } from "react";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { useLocation, useNavigate } from "@/lib/routing";
import { readOpenTilesOnLaunch } from "./tilesPreferences";

// Once per page load: later visits to the start page are deliberate.
let launchHandled = false;

/** Opens the Tile view at launch when the user chose "Open in Tiles". */
export function TilesLaunchRedirect({ prefix }: { prefix: string }) {
  const info = useServerInfo();
  const { pathname } = useLocation();
  const navigate = useNavigate();
  useEffect(() => {
    if (launchHandled || info === "loading") return;
    launchHandled = true;
    const onStartPage = pathname === (prefix || "/") || pathname === `${prefix}/`;
    if (onStartPage && isFeatureEnabled(info, "tiles_view") && readOpenTilesOnLaunch()) {
      navigate(`${prefix}/tiles`, { replace: true });
    }
  }, [info, pathname, prefix, navigate]);
  return null;
}

/** Test seam: allow the launch check to run again. */
export function resetTilesLaunchForTests() {
  launchHandled = false;
}
