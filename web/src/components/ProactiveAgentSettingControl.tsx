import { Fragment, type ReactNode, useState } from "react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import {
  useProactiveAgentSettings,
  useSetProactiveAgentSetting,
  type ProactiveAgentSettingScope,
} from "@/hooks/useProactiveAgentSettings";
import { PROACTIVE_AGENT_DISPLAY_NAME, PROACTIVE_AGENT_SCOPE_COPY } from "@/lib/proactiveInbox";
import type { SettingsEntry } from "@/pages/settings/search/catalog/types";
import { SettingsEntryLabel, SettingsItem } from "@/pages/settings/search/SettingsItem";
import { settingsControlProps } from "@/pages/settings/search/settingsControl";

// The row's other controls keep one identity as the settings load, so an edit in progress survives.
const ROW_CONTROLS_KEY = "row-controls";

/**
 * One scope's switch; `entry` is that scope's Settings row, e.g. the Omni Advisor row. `children`
 * are that row's other controls, such as an admin's prompt editor.
 */
export function ProactiveAgentSettingControl({
  scope,
  entry,
  children,
}: {
  scope: ProactiveAgentSettingScope;
  entry: SettingsEntry & { readonly parts: { readonly retry: string } };
  children?: ReactNode;
}) {
  const { available, settings, query } = useProactiveAgentSettings();
  const setSetting = useSetProactiveAgentSetting();
  const [error, setError] = useState<string | null>(null);
  if (!available) return null;
  if (!settings) {
    const retry = query.isError ? (
      <>
        <span role="alert" className="text-destructive">
          Couldn’t load {PROACTIVE_AGENT_DISPLAY_NAME} settings.
        </span>
        <Button
          {...settingsControlProps(entry, "retry")}
          variant="outline"
          size="sm"
          onClick={() => void query.refetch()}
          disabled={query.isFetching}
          componentId={`settings.proactive_agent.${scope}.retry`}
        >
          Retry
        </Button>
      </>
    ) : null;
    const loading = <p className="text-ui text-muted-foreground">Loading...</p>;
    // The row's other controls, such as the prompt editor, don't depend on this read.
    if (children) {
      return (
        <SettingsItem entry={entry} className="flex flex-col gap-6">
          {retry ? (
            <div className="flex items-center justify-between gap-6 text-ui">{retry}</div>
          ) : (
            loading
          )}
          <Fragment key={ROW_CONTROLS_KEY}>{children}</Fragment>
        </SettingsItem>
      );
    }
    if (retry) {
      return (
        <SettingsItem entry={entry} className="flex items-center justify-between gap-6 text-ui">
          {retry}
        </SettingsItem>
      );
    }
    return loading;
  }
  if (scope === "workspace" && !settings.can_manage_workspace) return null;
  const turnedOffByAdmin = scope === "me" && !settings.workspace_enabled;
  const checked =
    scope === "workspace" ? settings.workspace_enabled : settings.user_enabled && !turnedOffByAdmin;
  const description = turnedOffByAdmin
    ? `An admin turned ${entry.label} off for this workspace.`
    : PROACTIVE_AGENT_SCOPE_COPY[scope];
  return (
    <SettingsItem entry={entry} className="flex flex-col gap-6">
      <div className="flex items-start justify-between gap-6">
        <SettingsEntryLabel entry={entry} className="flex-1" description={description} />
        <Switch
          // The viewer's row names its switch as a part; the workspace's switch is its row's own.
          {...settingsControlProps(entry, "toggle" in entry.parts ? "toggle" : undefined)}
          checked={checked}
          disabled={turnedOffByAdmin || setSetting.isPending}
          onCheckedChange={(enabled) => {
            setError(null);
            setSetting.mutate({ scope, enabled }, { onError: (e) => setError(e.message) });
          }}
          data-testid={`proactive-agent-${scope}-toggle`}
          className="mt-0.5 shrink-0"
          componentId={`settings.proactive_agent.${scope}`}
        />
      </div>
      {error && (
        <p role="alert" className="text-ui text-destructive">
          {error}
        </p>
      )}
      <Fragment key={ROW_CONTROLS_KEY}>{children}</Fragment>
    </SettingsItem>
  );
}
