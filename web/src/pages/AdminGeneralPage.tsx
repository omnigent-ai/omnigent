import { useState } from "react";
import { PageScroll } from "@/components/PageScroll";
import { SettingsGroup } from "@/components/SettingsGroup";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import { useSharing, useSetSharing } from "@/hooks/useSharing";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isSingleUserMode, type SiblingSessionDestinations } from "@/lib/capabilities";

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

  if (!nestedAgentsEnabled) return null;
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
          {sharing && (
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
        </>
      )}
    </PageScroll>
  );
}
