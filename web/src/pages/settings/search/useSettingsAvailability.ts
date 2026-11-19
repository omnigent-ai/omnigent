// What decides which Settings rows show here, read from the same hooks and bridges the page's
// rows read, so search leaves out what the page doesn't render.

import { useEffect, useMemo, useState } from "react";

import { useIsAdmin } from "@/hooks/useIsAdmin";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { getDesktopFeatures, supportsBrowser, supportsGlobalShortcut } from "@/lib/nativeBridge";

import type { SettingsAvailability } from "./catalog/types";

export function useSettingsAvailability(): SettingsAvailability {
  const info = useServerInfo();
  const isAdmin = useIsAdmin();
  const loaded = info === "loading" ? null : info;
  const providers = loaded?.enabled_connections ?? [];
  const globalShortcut = supportsGlobalShortcut();
  const floatingComposerFlag = isFeatureEnabled(info, "floating_composer");
  // The shell answers asynchronously; until then the New Agent row counts as absent.
  const [shellTakesPanelShortcut, setShellTakesPanelShortcut] = useState(false);
  useEffect(() => {
    if (!globalShortcut || !floatingComposerFlag) return;
    let cancelled = false;
    void getDesktopFeatures().then((features) => {
      if (!cancelled) setShellTakesPanelShortcut(features?.floatingComposerShortcut === true);
    });
    return () => {
      cancelled = true;
    };
  }, [globalShortcut, floatingComposerFlag]);
  const floatingComposerShortcut =
    globalShortcut && floatingComposerFlag && shellTakesPanelShortcut;
  const inAppBrowser = supportsBrowser();
  const adminView = isAdmin && loaded?.admin_view !== false;
  const accountsEnabled = loaded?.accounts_enabled === true;
  const archiveWorktreeCleanup = loaded?.archive_worktree_cleanup === true;
  const mcpServices = providers.includes("mcp");
  const github = providers.includes("github");
  const databricks = providers.includes("databricks");
  return useMemo(
    () => ({
      globalShortcut,
      floatingComposerShortcut,
      inAppBrowser,
      adminView,
      accountsEnabled,
      archiveWorktreeCleanup,
      mcpServices,
      integrationProviders: { github, databricks },
    }),
    [
      globalShortcut,
      floatingComposerShortcut,
      inAppBrowser,
      adminView,
      accountsEnabled,
      archiveWorktreeCleanup,
      mcpServices,
      github,
      databricks,
    ],
  );
}
