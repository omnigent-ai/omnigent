import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";

import { Switch } from "@/components/ui/switch";
import { getWidenWindowForRightSidebar, setWidenWindowForRightSidebar } from "@/lib/nativeBridge";
import { WIDEN_WINDOW_FOR_RIGHT_SIDEBAR_ENTRY } from "./search/catalog/general";
import { SettingsEntryLabel, SettingsItem } from "./search/SettingsItem";
import { settingsControlProps } from "./search/settingsControl";

/** The desktop shell's opt-in to widen the window when the right sidebar opens. */
export function WidenWindowForRightSidebarControl() {
  // Null until the shell answers, or if it can't; the switch stays disabled meanwhile.
  const [enabled, setEnabled] = useState<boolean | null>(null);
  // One save at a time, so a failed save always rolls back to the shell's value.
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void getWidenWindowForRightSidebar().then((saved) => {
      if (cancelled) return;
      if (saved === null) toast.error("Couldn't load this setting.");
      else setEnabled(saved);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const toggle = useCallback(async (next: boolean) => {
    setEnabled(next);
    setSaving(true);
    const saved = await setWidenWindowForRightSidebar(next);
    setSaving(false);
    if (saved) return;
    setEnabled(!next);
    toast.error("Couldn't save this setting. Try again.");
  }, []);

  return (
    <SettingsItem
      entry={WIDEN_WINDOW_FOR_RIGHT_SIDEBAR_ENTRY}
      className="flex items-start justify-between gap-6"
    >
      <SettingsEntryLabel entry={WIDEN_WINDOW_FOR_RIGHT_SIDEBAR_ENTRY} className="flex-1" />
      <Switch
        {...settingsControlProps(WIDEN_WINDOW_FOR_RIGHT_SIDEBAR_ENTRY)}
        checked={enabled === true}
        disabled={enabled === null || saving}
        onCheckedChange={(next) => void toggle(next)}
        data-testid="widen-window-for-right-sidebar-toggle"
        className="mt-0.5 shrink-0"
        componentId="settings.general.widen_window_for_right_sidebar"
      />
    </SettingsItem>
  );
}
