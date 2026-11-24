// What decides which Settings rows show here, read from the same hooks and bridges the page's
// rows read, so search leaves out what the page doesn't render.

import { useEffect, useMemo, useState } from "react";

import { useAdminViewSwitchShown } from "@/hooks/useAdminViewSwitchShown";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { desktopAppHandoffSupported } from "@/lib/desktopAppHandoff";
import { isFeatureEnabled } from "@/lib/capabilities";
import {
  getDesktopFeatures,
  supportsBrowser,
  supportsGlobalShortcut,
  supportsWidenWindowForRightSidebar,
} from "@/lib/nativeBridge";

import type { SettingsAvailability } from "./catalog/types";

export function useSettingsAvailability(): SettingsAvailability {
  const info = useServerInfo();
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
  const widenWindowForRightSidebar = supportsWidenWindowForRightSidebar();
  const inAppBrowser = supportsBrowser();
  const desktopAppHandoff = desktopAppHandoffSupported();
  const adminView = useAdminViewSwitchShown();
  const accountsEnabled = loaded?.accounts_enabled === true;
  const archiveWorktreeCleanup = loaded?.archive_worktree_cleanup === true;
  const mcpServices = providers.includes("mcp");
  const github = providers.includes("github");
  const databricks = providers.includes("databricks");
  const projectKanban = isFeatureEnabled(info, "project_kanban");
  const sessionTitleInstructions = loaded?.session_title_instructions === true;
  const proactiveAgent = loaded?.proactive_agent_available === true;
  return useMemo(
    () => ({
      globalShortcut,
      floatingComposerShortcut,
      widenWindowForRightSidebar,
      inAppBrowser,
      desktopAppHandoff,
      adminView,
      accountsEnabled,
      archiveWorktreeCleanup,
      mcpServices,
      integrationProviders: { github, databricks },
      projectKanban,
      sessionTitleInstructions,
      proactiveAgent,
    }),
    [
      globalShortcut,
      floatingComposerShortcut,
      widenWindowForRightSidebar,
      inAppBrowser,
      desktopAppHandoff,
      adminView,
      accountsEnabled,
      archiveWorktreeCleanup,
      mcpServices,
      github,
      databricks,
      projectKanban,
      sessionTitleInstructions,
      proactiveAgent,
    ],
  );
}
