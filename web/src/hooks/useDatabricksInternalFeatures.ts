import { useEffect, useState } from "react";
import { getDesktopFeatures, isElectronShell } from "@/lib/nativeBridge";

/**
 * The desktop shell's Databricks-internal MDM flag, already scoped by the shell
 * to windows on a Databricks-managed server. Read once per mount; false in a browser.
 */
export function useDatabricksInternalFeatures(): boolean {
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    if (!isElectronShell()) return;
    let cancelled = false;
    void getDesktopFeatures().then((features) => {
      if (!cancelled) setEnabled(features?.databricksInternalFeatures === true);
    });
    return () => {
      cancelled = true;
    };
  }, []);
  return enabled;
}
