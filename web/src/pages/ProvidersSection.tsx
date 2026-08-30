import { useState, type ReactNode } from "react";
import {
  AlertTriangleIcon,
  CableIcon,
  CircleAlertIcon,
  CircleCheckIcon,
  CircleDashedIcon,
  Loader2Icon,
  RefreshCwIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  useHosts,
  useProviderInventory,
  type ProviderCapabilitySupport,
  type ProviderConnectionState,
  type ProviderInventoryEntry,
} from "@/hooks/useHosts";
import { useHarnessSetupSteps } from "@/lib/agentLabels";
import { HarnessSetupDialog } from "@/shell/HarnessSetupDialog";

/** CLI-backed rows reuse the existing host setup checklist when available. */
function providerManageHarness(
  provider: ProviderInventoryEntry,
  setupStepsByHarness: Record<string, unknown>,
): string | null {
  if (!provider.cli) return null;
  return setupStepsByHarness[provider.cli] ? provider.cli : null;
}

function CapabilityChip({ label, state }: { label: string; state: ProviderCapabilitySupport }) {
  const Icon =
    state === "supported"
      ? CircleCheckIcon
      : state === "unknown"
        ? CircleDashedIcon
        : CircleAlertIcon;
  return (
    <span
      className={`inline-flex items-center gap-1 text-xs ${
        state === "supported" ? "text-foreground" : "text-muted-foreground"
      }`}
      data-testid={`provider-capability-${label.toLowerCase().replace(/\s+/g, "-")}`}
      title={
        state === "supported"
          ? "Supported"
          : state === "unknown"
            ? "Unknown — Omnigent cannot determine this locally"
            : "Not supported by this provider integration"
      }
    >
      <Icon
        className={`size-3.5 shrink-0 ${
          state === "supported" ? "text-emerald-600 dark:text-emerald-500" : ""
        }`}
      />
      {label}
      {state === "unknown" ? " (?)" : ""}
    </span>
  );
}

const CONNECTION_PRESENTATION: Record<
  ProviderConnectionState,
  { label: string; className: string; iconClassName: string }
> = {
  connected: {
    label: "Credential available locally",
    className: "text-emerald-700 dark:text-emerald-400",
    iconClassName: "text-emerald-600 dark:text-emerald-500",
  },
  authentication_required: {
    label: "Authentication required",
    className: "text-amber-700 dark:text-amber-400",
    iconClassName: "text-amber-600 dark:text-amber-500",
  },
  misconfigured: {
    label: "Misconfigured",
    className: "text-amber-700 dark:text-amber-400",
    iconClassName: "text-amber-600 dark:text-amber-500",
  },
  unavailable: {
    label: "Unavailable on this host",
    className: "text-destructive",
    iconClassName: "text-destructive",
  },
  unknown: {
    label: "Status unknown",
    className: "text-muted-foreground",
    iconClassName: "text-muted-foreground",
  },
};

function ProviderConnectionStatus({ provider }: { provider: ProviderInventoryEntry }) {
  const presentation = CONNECTION_PRESENTATION[provider.connection_state];
  const Icon =
    provider.connection_state === "connected"
      ? CircleCheckIcon
      : provider.connection_state === "unknown"
        ? CircleDashedIcon
        : CircleAlertIcon;
  return (
    <div className="mt-2" data-testid={`provider-connection-${provider.id}`}>
      <div className={`flex items-center gap-1.5 text-sm font-medium ${presentation.className}`}>
        <Icon className={`size-4 shrink-0 ${presentation.iconClassName}`} />
        <span>{presentation.label}</span>
      </div>
      <p className="mt-0.5 text-sm text-muted-foreground">{provider.connection_detail}</p>
    </div>
  );
}

function ProviderRow({
  provider,
  manageHarness,
  onManage,
}: {
  provider: ProviderInventoryEntry;
  manageHarness: string | null;
  onManage: (harness: string) => void;
}) {
  const valid = provider.configuration_state === "valid";
  return (
    <div
      data-testid={`provider-row-${provider.id}`}
      className="rounded-lg border border-border bg-card px-4 py-3"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <CableIcon className="ui-icon shrink-0 text-muted-foreground" />
            <span className="font-medium">{provider.display_name}</span>
            <Badge variant="secondary" className="lowercase">
              {provider.kind}
            </Badge>
            {provider.origin === "detected" && <Badge variant="outline">Detected</Badge>}
          </div>

          <ProviderConnectionStatus provider={provider} />

          {!valid && (
            <div className="mt-2 text-sm text-amber-700 dark:text-amber-400">
              <div className="flex items-center gap-1.5 font-medium">
                <CircleAlertIcon className="size-4 shrink-0" />
                <span>Configuration invalid</span>
              </div>
              {provider.error && <p className="mt-0.5 text-muted-foreground">{provider.error}</p>}
            </div>
          )}

          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted-foreground">
            {provider.families.length > 0 && (
              <span>
                Families:{" "}
                {provider.families
                  .map(
                    (family) =>
                      `${family}${provider.default_models[family] ? ` (${provider.default_models[family]})` : ""}`,
                  )
                  .join(", ")}
              </span>
            )}
            {provider.cli && <span>CLI: {provider.cli}</span>}
            {provider.profile && <span>Profile: {provider.profile}</span>}
            {provider.model_provider && <span>Gateway: {provider.model_provider}</span>}
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
            <CapabilityChip label="Model discovery" state={provider.capabilities.model_discovery} />
            <CapabilityChip
              label="Multiple profiles"
              state={provider.capabilities.multiple_profiles}
            />
            <CapabilityChip label="Interactive CLI" state={provider.capabilities.interactive_cli} />
          </div>
        </div>
        {manageHarness && (
          <Button
            variant="outline"
            size="sm"
            className="shrink-0"
            onClick={() => onManage(manageHarness)}
            data-testid={`provider-manage-${provider.id}`}
          >
            Manage
          </Button>
        )}
      </div>
    </div>
  );
}

function ProvidersSectionShell({ children }: { children: ReactNode }) {
  return (
    <section>
      <h1 className="text-2xl font-semibold">Providers</h1>
      <p className="mt-1 text-ui text-muted-foreground">
        Provider configuration and local credential evidence for the selected host. Opening this
        page never contacts a remote provider or opens a protected credential store.
      </p>
      <div className="mt-6">{children}</div>
    </section>
  );
}

/** Read-only provider inventory for the selected connected host. */
export function ProvidersSection() {
  const { data: hosts, isLoading: hostsLoading } = useHosts();
  const hostsList = hosts ?? [];
  const [selectedHostId, setSelectedHostId] = useState<string | null>(null);
  const host = hostsList.find((candidate) => candidate.host_id === selectedHostId) ?? hostsList[0];
  const inventory = useProviderInventory(host?.host_id, !!host);
  const setupStepsByHarness = useHarnessSetupSteps();
  const [manageHarness, setManageHarness] = useState<string | null>(null);

  if (hostsLoading) {
    return (
      <ProvidersSectionShell>
        <p className="flex items-center gap-2 text-muted-foreground">
          <Loader2Icon className="size-4 animate-spin" />
          Loading hosts…
        </p>
      </ProvidersSectionShell>
    );
  }

  if (hostsList.length === 0) {
    return (
      <ProvidersSectionShell>
        <p className="text-muted-foreground">
          No host is connected yet. Start one with <code>omnigent host</code> to see its providers
          here.
        </p>
      </ProvidersSectionShell>
    );
  }

  const providers = inventory.data ?? [];

  return (
    <ProvidersSectionShell>
      <div className="flex flex-col gap-4">
        <div className="flex items-center gap-2">
          {hostsList.length > 1 && (
            <Select
              value={host?.host_id ?? ""}
              onValueChange={(id) => {
                setManageHarness(null);
                setSelectedHostId(id);
              }}
              name="provider-host"
            >
              <SelectTrigger data-testid="provider-host-select" className="w-56" aria-label="Host">
                <SelectValue placeholder="Host" />
              </SelectTrigger>
              <SelectContent>
                {hostsList.map((candidate) => (
                  <SelectItem key={candidate.host_id} value={candidate.host_id}>
                    {candidate.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          {hostsList.length === 1 && (
            <span className="text-sm text-muted-foreground">Host: {host?.name}</span>
          )}
          <Button
            variant="ghost"
            size="sm"
            className="gap-1.5"
            onClick={() => void inventory.refetch()}
            disabled={inventory.isFetching}
            data-testid="provider-refresh"
          >
            <RefreshCwIcon className={`size-3.5 ${inventory.isFetching ? "animate-spin" : ""}`} />
            Refresh
          </Button>
        </div>

        {inventory.isLoading && (
          <p
            className="flex items-center gap-2 text-muted-foreground"
            data-testid="provider-loading"
          >
            <Loader2Icon className="size-4 animate-spin" />
            Loading providers…
          </p>
        )}

        {inventory.isError && (
          <div
            className="rounded-lg border border-border bg-card px-4 py-3 text-sm"
            data-testid="provider-error"
          >
            <div className="flex items-center gap-1.5">
              <AlertTriangleIcon className="size-4 shrink-0 text-amber-600 dark:text-amber-500" />
              <span>Couldn&apos;t load providers.</span>
            </div>
            <p className="mt-1 text-muted-foreground">
              {inventory.error instanceof Error ? inventory.error.message : "Unknown error"}
            </p>
            <Button
              variant="outline"
              size="sm"
              className="mt-2"
              onClick={() => void inventory.refetch()}
            >
              Retry
            </Button>
          </div>
        )}

        {!inventory.isLoading && !inventory.isError && providers.length === 0 && (
          <p className="text-muted-foreground" data-testid="provider-empty">
            No providers configured or detected on this host yet. Run <code>omni setup</code> to
            configure one.
          </p>
        )}

        {!inventory.isError &&
          providers.map((provider) => (
            <ProviderRow
              key={provider.id}
              provider={provider}
              manageHarness={providerManageHarness(provider, setupStepsByHarness)}
              onManage={setManageHarness}
            />
          ))}
      </div>

      <HarnessSetupDialog
        open={manageHarness !== null}
        onOpenChange={(open) => {
          if (!open) setManageHarness(null);
        }}
        agentName={manageHarness ?? undefined}
        harness={manageHarness}
        host={host}
      />
    </ProvidersSectionShell>
  );
}
