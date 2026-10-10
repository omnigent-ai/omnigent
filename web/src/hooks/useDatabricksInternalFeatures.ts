import { useEffect, useState } from "react";
import { getDesktopFeatures, isElectronShell } from "@/lib/nativeBridge";

/**
 * The desktop shell's Databricks-internal MDM flag, already scoped by the shell
 * to windows on a Databricks-managed server. Read once per mount; `null` while
 * the shell's answer is pending, `false` outright in a browser.
 */
export function useDatabricksInternalFeatures(): boolean | null {
  const [enabled, setEnabled] = useState<boolean | null>(() => (isElectronShell() ? null : false));
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
