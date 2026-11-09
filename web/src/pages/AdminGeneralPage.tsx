import { useState } from "react";
import { PageScroll } from "@/components/PageScroll";
import { SettingsGroup } from "@/components/SettingsGroup";
import { SettingsLabel } from "@/components/SettingsLabel";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import { useProactiveAgents, useSetProactiveAgents } from "@/hooks/useProactiveAgents";
import { useSharing, useSetSharing } from "@/hooks/useSharing";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  isFeatureEnabled,
  isSingleUserMode,
  type SiblingSessionDestinations,
} from "@/lib/capabilities";

const AGENT_DESTINATIONS: {
  id: SiblingSessionDestinations;
  label: string;
  description: string;
}[] = [
  {
    id: "current_host",
    label: "Current host only",
    description: "New agents must run on the same host as the session they are added from.",
  },
  {
    id: "current_host_or_new_sandbox",
    label: "Current host or new sandbox",
    description: "New agents can use the current host or start in a fresh managed sandbox.",
  },
  {
    id: "any_host",
    label: "Any available host",
    description:
      "New agents can use the current host, another connected host, or a fresh managed sandbox.",
  },
];

export function AdminGeneralPage() {
  const info = useServerInfo();
  const isAdmin = useIsAdmin();
  const canManage = isSingleUserMode(info) || isAdmin;
  const nestedAgentsEnabled = info !== "loading" && info.sibling_sessions === true;
  const proactiveEnabled = isFeatureEnabled(info, "proactive_agent");
  const {
    data: sharing,
    error: sharingError,
    isLoading,
  } = useSharing({ enabled: canManage && nestedAgentsEnabled });
  const setSharing = useSetSharing();
  const [destinationError, setDestinationError] = useState<string | null>(null);
  const destinations = sharing?.sibling_session_destinations ?? "any_host";
  const destination = AGENT_DESTINATIONS.find((option) => option.id === destinations);

  function chooseDestinations(value: SiblingSessionDestinations) {
    if (
      value === destinations ||
      !sharing?.sibling_session_destinations_editable ||
      setSharing.isPending
    )
      return;
    setDestinationError(null);
    setSharing.mutate(
      { sibling_session_destinations: value },
      { onError: (error) => setDestinationError(error.message) },
    );
  }

  if (!nestedAgentsEnabled && !proactiveEnabled) return null;
  return (
    <PageScroll maxWidthClassName="max-w-4xl" contentClassName="px-4 md:px-8" extraBottom="2.5rem">
      <h1 className="settings-page-title text-2xl font-medium">Admin settings</h1>
      <p className="mt-1 mb-6 text-sm text-muted-foreground">Server-wide settings for all users.</p>
      {!canManage ? (
        <p className="text-ui text-muted-foreground">
          You don't have permission to manage server settings.
        </p>
      ) : (
        <>
          {isLoading && <p className="text-ui text-muted-foreground">Loading...</p>}
          {nestedAgentsEnabled && sharing && (
            <SettingsGroup
              title="Nested agent destinations"
              testId="settings-group-agent-destinations"
            >
              <div className="flex flex-col gap-3 md:flex-row-reverse md:items-start md:gap-6">
                <Select
                  value={destinations}
                  onValueChange={(value) => chooseDestinations(value as SiblingSessionDestinations)}
                  disabled={!sharing.sibling_session_destinations_editable || setSharing.isPending}
                  componentId="settings.admin.agent_destinations"
                  valueHasNoPii
                >
                  <SelectTrigger
                    className="w-full shrink-0 md:w-64"
                    aria-label="Nested agent destinations"
                    aria-describedby="agent-destinations-description"
                  >
                    <SelectValue>{destination?.label ?? destinations}</SelectValue>
                  </SelectTrigger>
                  <SelectContent
                    position="popper"
                    align="end"
                    className="w-[30rem] max-w-[calc(100vw-2rem)]"
                  >
                    {AGENT_DESTINATIONS.filter(
                      (option) =>
                        sharing.sibling_session_destinations_options?.includes(option.id) ?? true,
                    ).map((option) => (
                      <SelectItem key={option.id} value={option.id} textValue={option.label}>
                        <span className="flex flex-col gap-1">
                          <span className="font-medium">{option.label}</span>
                          <span className="text-sm leading-normal whitespace-normal text-muted-foreground">
                            {option.description}
                          </span>
                        </span>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p
                  id="agent-destinations-description"
                  className="min-w-0 flex-1 text-sm leading-normal text-muted-foreground md:pt-1"
                >
                  {destination?.description}
                </p>
              </div>
            </SettingsGroup>
          )}
          {(destinationError || sharingError) && (
            <div
              role="alert"
              className="mt-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-ui text-destructive"
            >
              {destinationError ?? sharingError?.message}
            </div>
          )}
          {proactiveEnabled && <ProactiveAgentsSection />}
        </>
      )}
    </PageScroll>
  );
}

/** Turns the built-in proactive agents on or off for every user. */
function ProactiveAgentsSection() {
  const { data, error } = useProactiveAgents();
  const setProactive = useSetProactiveAgents();
  const [saveError, setSaveError] = useState<string | null>(null);
  if (!data) {
    return error ? (
      <p role="alert" className="mt-6 text-ui text-destructive">
        {error.message}
      </p>
    ) : null;
  }
  const disabled = !data.editable || setProactive.isPending;
  function toggle(name: string, enabled: boolean) {
    setSaveError(null);
    // Turning an agent on also turns proactive agents on; turning it off leaves the others.
    const update = enabled
      ? { enabled: true, agents: { [name]: true } }
      : { agents: { [name]: false } };
    setProactive.mutate(update, { onError: (err) => setSaveError(err.message) });
  }
  return (
    <div className="mt-6">
      <SettingsGroup title="Proactive agents" testId="settings-group-proactive-agents">
        {data.agents.map((agent) => (
          <div key={agent.name} className="flex items-center justify-between gap-4">
            <SettingsLabel
              label={agent.label}
              className="flex-1"
              description={
                <>
                  {agent.description} Runs in a new sandbox session for each user.
                  {!data.editable && (
                    <span className="mt-1 block">
                      Managed by this deployment and can&apos;t be changed here.
                    </span>
                  )}
                </>
              }
            />
            <Switch
              checked={data.enabled && agent.enabled}
              onCheckedChange={(checked) => toggle(agent.name, checked)}
              disabled={disabled}
              aria-label={agent.label}
              componentId="settings.admin.proactive_agents"
            />
          </div>
        ))}
      </SettingsGroup>
      {saveError && (
        <div
          role="alert"
          className="mt-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-ui text-destructive"
        >
          {saveError}
        </div>
      )}
    </div>
  );
}
