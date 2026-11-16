import { useCallback, useId, useState } from "react";
import { SettingsGroup } from "@/components/SettingsGroup";
import { SettingsLabel } from "@/components/SettingsLabel";
import { Switch } from "@/components/ui/switch";
import { supportsBrowser } from "@/lib/nativeBridge";
import {
  desktopSupportsBrowserAgentTools,
  readBrowserAgentToolsEnabled,
  writeBrowserAgentToolsEnabled,
} from "./preferences";
import { resetMainBrowserTabs } from "./tabStore";

export const UPDATE_DESKTOP_NOTE =
  "Needs a newer Omnigent desktop app. Update the app to turn this on.";

/** Settings > General > Browser: the advanced browser tools switch. Desktop
 *  apps with the embedded browser only; unavailable in ones without the
 *  tools (older apps), which run the standard browser tools. */
export function BrowserAgentToolsSettingsGroup() {
  const supported = desktopSupportsBrowserAgentTools();
  const [enabled, setEnabled] = useState(() => supported && readBrowserAgentToolsEnabled());
  const labelId = useId();
  const descriptionId = useId();
  const toggle = useCallback((next: boolean) => {
    writeBrowserAgentToolsEnabled(next);
    // Show what was stored: a failed write leaves the previous mode in effect.
    const stored = readBrowserAgentToolsEnabled();
    setEnabled(stored);
    // Off: the tabs go back to the saved workspace, a page per window.
    if (!stored) void resetMainBrowserTabs().catch(() => {});
  }, []);

  if (!supportsBrowser()) return null;
  return (
    <SettingsGroup title="Browser" testId="settings-group-browser-agent-tools">
      <div className="flex items-start justify-between gap-6">
        <SettingsLabel
          label="Advanced browser tools for agents"
          labelId={labelId}
          descriptionId={descriptionId}
          className="flex-1"
          description={
            supported
              ? "Let agents go back and forward, reload, manage tabs, hover, right-click, double-click, drag, scroll, press keys, and use dropdowns and checkboxes in the in-app browser. New sessions use this setting. Turning it off also stops these actions in existing sessions on this device."
              : UPDATE_DESKTOP_NOTE
          }
          descriptionClassName="text-ui"
        />
        <Switch
          aria-labelledby={labelId}
          aria-describedby={descriptionId}
          checked={enabled}
          disabled={!supported}
          onCheckedChange={toggle}
          data-testid="browser-agent-tools-toggle"
          className="mt-0.5 shrink-0"
          componentId="settings.general.browser_agent_tools"
        />
      </div>
    </SettingsGroup>
  );
}
