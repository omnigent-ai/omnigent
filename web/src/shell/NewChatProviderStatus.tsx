/** Read-only provider status for the New Chat configuration dialog. */
import { AlertTriangleIcon, CircleCheckIcon, CircleDashedIcon } from "lucide-react";

import { ConfigRow } from "@/components/HarnessConfigControls";
import {
  useProviderInventory,
  type Host,
  type ProviderConnectionState,
  type ProviderInventoryEntry,
} from "@/hooks/useHosts";

interface StatePresentation {
  label: string;
  className: string;
  icon: typeof CircleCheckIcon;
}

const STATE_PRESENTATION: Record<ProviderConnectionState, StatePresentation> = {
  connected: {
    label: "Credential available locally",
    className: "text-emerald-600 dark:text-emerald-500",
    icon: CircleCheckIcon,
  },
  authentication_required: {
    label: "Authentication required",
    className: "text-amber-600 dark:text-amber-500",
    icon: AlertTriangleIcon,
  },
  misconfigured: {
    label: "Misconfigured",
    className: "text-destructive",
    icon: AlertTriangleIcon,
  },
  unavailable: {
    label: "Unavailable on this host",
    className: "text-destructive",
    icon: AlertTriangleIcon,
  },
  unknown: {
    label: "Status unknown",
    className: "text-muted-foreground",
    icon: CircleDashedIcon,
  },
};

/** Return the host-selected provider for a harness, without guessing by family. */
export function providerForHarness(
  providers: readonly ProviderInventoryEntry[] | undefined,
  harness: string | null,
): ProviderInventoryEntry | null {
  if (!harness || !providers) return null;
  return providers.find((provider) => provider.default_for_harnesses?.includes(harness)) ?? null;
}

/**
 * Name the local provider that would serve the selected harness.
 *
 * The row is absent until the host supplies a complete answer. This prevents
 * an empty configuration row during host changes and for sandbox sessions.
 */
export function NewChatProviderStatus({
  host,
  harness,
  open,
}: {
  host: Host | null | undefined;
  harness: string | null;
  open: boolean;
}) {
  const hostId = host?.host_id ?? null;
  const inventory = useProviderInventory(hostId, open && !!hostId && !!harness);

  if (!open || !hostId || !harness || inventory.isLoading || inventory.isError) return null;

  const provider = providerForHarness(inventory.data, harness);
  if (!provider) return null;

  const presentation = STATE_PRESENTATION[provider.connection_state];
  const Icon = presentation.icon;
  return (
    <ConfigRow
      label="Provider"
      description="Selected locally for this harness"
      controlClassName="sm:w-80"
    >
      <div data-testid="new-chat-provider-status" data-state={provider.connection_state}>
        <div className="flex flex-wrap items-center gap-1.5 text-ui">
          <Icon className={`size-4 shrink-0 ${presentation.className}`} />
          <span className="font-medium">{provider.display_name}</span>
          <span className={`text-sm ${presentation.className}`}>{presentation.label}</span>
        </div>
        <p className="mt-0.5 text-sm text-muted-foreground">{provider.connection_detail}</p>
      </div>
    </ConfigRow>
  );
}
