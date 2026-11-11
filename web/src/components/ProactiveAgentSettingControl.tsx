import { useId, useState } from "react";
import { SettingsLabel } from "@/components/SettingsLabel";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import {
  useProactiveAgentSettings,
  useSetProactiveAgentSetting,
  type ProactiveAgentSettingScope,
} from "@/hooks/useProactiveAgentSettings";
import { PROACTIVE_AGENT_DISPLAY_NAME } from "@/lib/proactiveInbox";

const COPY: Record<ProactiveAgentSettingScope, string> = {
  me: `${PROACTIVE_AGENT_DISPLAY_NAME} reads your recent sessions on a schedule and posts follow-ups, related information, and reminders to your Inbox.`,
  workspace: `Let ${PROACTIVE_AGENT_DISPLAY_NAME} post follow-ups, related information, and reminders to each user's Inbox. Users can turn it off for themselves.`,
};

/**
 * On/off switch for the proactive agent (Omni Advisor): the viewer's own
 * setting, or the workspace-wide one for an admin. Renders nothing where the
 * server does not offer the agent, or for a workspace switch the viewer
 * cannot manage. While the settings load it says so, and a failed initial
 * read offers a retry instead of dropping the switch from the page.
 */
export function ProactiveAgentSettingControl({ scope }: { scope: ProactiveAgentSettingScope }) {
  const { available, settings, query } = useProactiveAgentSettings();
  const setSetting = useSetProactiveAgentSetting();
  const [error, setError] = useState<string | null>(null);
  const labelId = useId();
  if (!available) return null;
  if (!settings) {
    if (query.isError) {
      return (
        <div
          role="alert"
          className="flex items-center justify-between gap-6 text-ui text-destructive"
        >
          <span>Couldn’t load {PROACTIVE_AGENT_DISPLAY_NAME} settings.</span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void query.refetch()}
            disabled={query.isFetching}
            componentId={`settings.proactive_agent.${scope}.retry`}
          >
            Retry
          </Button>
        </div>
      );
    }
    return <p className="text-ui text-muted-foreground">Loading...</p>;
  }
  if (scope === "workspace" && !settings.can_manage_workspace) return null;
  const turnedOffByAdmin = scope === "me" && !settings.workspace_enabled;
  const checked =
    scope === "workspace" ? settings.workspace_enabled : settings.user_enabled && !turnedOffByAdmin;
  const description = turnedOffByAdmin
    ? `An admin turned ${PROACTIVE_AGENT_DISPLAY_NAME} off for this workspace.`
    : COPY[scope];
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-start justify-between gap-6">
        <SettingsLabel
          label={PROACTIVE_AGENT_DISPLAY_NAME}
          labelId={labelId}
          className="flex-1"
          description={description}
        />
        <Switch
          aria-labelledby={labelId}
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
    </div>
  );
}
