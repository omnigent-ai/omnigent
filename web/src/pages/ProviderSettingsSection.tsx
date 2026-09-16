import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangleIcon,
  ArrowLeftIcon,
  ChevronRightIcon,
  ChevronDownIcon,
  CloudIcon,
  KeyRoundIcon,
  Loader2Icon,
  PlusIcon,
  ServerCogIcon,
  TerminalIcon,
  Trash2Icon,
  WandSparklesIcon,
} from "lucide-react";

import { ProviderSetupTerminal } from "@/components/ProviderSetupTerminal";
import { iconForAgent } from "@/components/AgentCard";
import { ClaudeIcon } from "@/components/icons/ClaudeIcon";
import { CodexIcon } from "@/components/icons/CodexIcon";
import { CursorIcon } from "@/components/icons/CursorIcon";
import { OpenCodeIcon } from "@/components/icons/OpenCodeIcon";
import { PiIcon } from "@/components/icons/PiIcon";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useHosts, useInstallHarness, type Host } from "@/hooks/useHosts";
import { isFeatureEnabled } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  detectSetup,
  fetchSetupOperation,
  fetchSetupInventory,
  runSetupAction,
  startSetupOperation,
  type SetupAction,
  type SetupAcpAgent,
  type SetupDetection,
  type SetupDetectRequest,
  type SetupImportPreview,
  type SetupInventory,
  type SetupKeyProvider,
  type SetupOperation,
  type SetupOperationAction,
  type SetupProvider,
  type ProviderSurface,
  type SetupHarnessStatus,
  type SetupStatusHarness,
} from "@/lib/providerSetupApi";
import { useHarnessSetupSteps } from "@/lib/agentLabels";
import { harnessInstallableOnHost, resolveSetupSteps } from "@/lib/harnessSetup";
import { cn } from "@/lib/utils";

const REMEMBERED_HOST_KEY = "omnigent:provider-settings-host";
const REMEMBERED_HOST_NAME_KEY = `${REMEMBERED_HOST_KEY}:name`;
const SETUP_OPERATION_KEY_PREFIX = "omnigent:provider-setup-operation:";

const SETUP_LABELS: Record<string, string> = {
  anthropic: "Anthropic",
  openai: "OpenAI",
  gemini: "Gemini",
  pi: "Pi",
  environment: "environment variable",
  stored: "stored secret",
  inline: "inline value",
  command: "login command",
};

function setupLabel(value: string): string {
  return SETUP_LABELS[value] ?? value.replaceAll("_", " ");
}

function readRememberedHost(): string | null {
  try {
    return localStorage.getItem(REMEMBERED_HOST_KEY);
  } catch {
    return null;
  }
}

function readRememberedHostName(): string | null {
  try {
    return localStorage.getItem(REMEMBERED_HOST_NAME_KEY);
  } catch {
    return null;
  }
}

function rememberHost(hostId: string, name: string): void {
  try {
    localStorage.setItem(REMEMBERED_HOST_KEY, hostId);
    localStorage.setItem(REMEMBERED_HOST_NAME_KEY, name);
  } catch {
    // Storage can be unavailable in privacy-restricted embeds.
  }
}

function setupOperationStorageKey(hostId: string): string {
  return `${SETUP_OPERATION_KEY_PREFIX}${hostId}`;
}

function setupOperationAgentKey(hostId: string): string {
  return `${setupOperationStorageKey(hostId)}:agent`;
}

function readSetupOperationAgent(hostId: string): string | null {
  try {
    return sessionStorage.getItem(setupOperationAgentKey(hostId));
  } catch {
    return null;
  }
}

function readSetupOperationId(hostId: string): string | null {
  try {
    return sessionStorage.getItem(setupOperationStorageKey(hostId));
  } catch {
    return null;
  }
}

function rememberSetupOperation(hostId: string, operationId: string): void {
  try {
    sessionStorage.setItem(setupOperationStorageKey(hostId), operationId);
  } catch {
    // The mounted page still retains the operation when storage is unavailable.
  }
}

function forgetSetupOperation(hostId: string): void {
  try {
    sessionStorage.removeItem(setupOperationStorageKey(hostId));
    sessionStorage.removeItem(setupOperationAgentKey(hostId));
  } catch {
    // Storage can be unavailable in privacy-restricted embeds.
  }
}

export function ProviderSettingsSection() {
  const hostsQuery = useHosts({ refetchOnFocus: true });
  const hosts = hostsQuery.data;
  const [hostId, setHostId] = useState<string | null>(null);

  useEffect(() => {
    if (!hosts) return;
    if (hostId) return;
    const remembered = readRememberedHost();
    if (remembered) {
      setHostId(remembered);
      return;
    }
    if (hosts.length === 1) {
      setHostId(hosts[0].host_id);
    }
  }, [hostId, hosts]);

  const host = hosts?.find((candidate) => candidate.host_id === hostId) ?? null;
  const rememberedHostName = readRememberedHostName();
  const selectedHostMissing = !!hostId && !!hosts && !host;

  return (
    <section>
      <h1 className="text-2xl font-semibold">Providers</h1>
      <p className="mt-1 text-ui text-muted-foreground">
        Configure model providers and coding-agent authentication on a computer.
      </p>
      <div className="mt-5 flex flex-col gap-4">
        <div className="border-b border-border pb-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-ui font-medium">Computer</h2>
            </div>
            {hosts && hosts.length > 0 && (
              <Select
                value={hostId ?? undefined}
                onValueChange={(next) => {
                  setHostId(next);
                  const selected = hosts.find((candidate) => candidate.host_id === next);
                  rememberHost(next, selected?.name ?? next);
                }}
                componentId="settings.providers.host"
                valueHasNoPii={false}
              >
                <SelectTrigger
                  aria-label="Computer"
                  className="w-64"
                  data-testid="settings-providers-host"
                >
                  <SelectValue placeholder="Choose a computer" />
                </SelectTrigger>
                <SelectContent>
                  {selectedHostMissing && (
                    <SelectItem value={hostId} disabled>
                      {rememberedHostName ?? hostId} · unavailable
                    </SelectItem>
                  )}
                  {hosts.map((candidate) => (
                    <SelectItem key={candidate.host_id} value={candidate.host_id}>
                      {candidate.name} · {candidate.status}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>
          {hostsQuery.isPending && (
            <p className="mt-3 flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2Icon className="size-4 animate-spin" /> Loading computers…
            </p>
          )}
          {hostsQuery.isError && (
            <InlineError
              message="Computers are unavailable."
              onRetry={() => void hostsQuery.refetch()}
            />
          )}
          {hosts && hosts.length === 0 && !selectedHostMissing && (
            <p className="mt-3 text-sm text-muted-foreground">
              Connect a computer with{" "}
              <code className="rounded bg-muted px-1 py-0.5">omni host</code> before configuring
              providers.
            </p>
          )}
          {hosts && hosts.length > 1 && !host && (
            <p className="mt-3 text-sm text-muted-foreground">
              {selectedHostMissing
                ? `${rememberedHostName ?? "The selected computer"} is unavailable. Choose another computer explicitly to change this selection.`
                : "Choose a computer. Omnigent will never move these settings to another computer automatically."}
            </p>
          )}
          {selectedHostMissing && hosts && hosts.length <= 1 && (
            <p className="mt-3 text-sm text-muted-foreground">
              {rememberedHostName ?? "The selected computer"} is unavailable. This page will not
              switch to another computer automatically.
            </p>
          )}
        </div>

        {host?.status === "offline" && (
          <div
            role="status"
            className="rounded-xl border border-amber-500/35 bg-amber-500/10 p-4 text-sm"
          >
            <div className="flex items-start gap-2">
              <AlertTriangleIcon className="mt-0.5 size-4 shrink-0 text-amber-600" />
              <span>
                {host.name} is offline. Its settings cannot be read or changed until it reconnects;
                this selection will stay on {host.name}.
              </span>
            </div>
          </div>
        )}

        {host?.status === "online" && <ProviderHostSettings key={host.host_id} host={host} />}
      </div>
    </section>
  );
}

function InlineError({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="mt-3 flex flex-wrap items-center gap-2 text-sm text-destructive">
      <AlertTriangleIcon className="size-4" />
      <span>{message}</span>
      {onRetry && (
        <Button type="button" variant="outline" size="sm" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

function ProviderHostSettings({ host }: { host: Host }) {
  const queryClient = useQueryClient();
  const info = useServerInfo();
  const queryKey = ["provider-setup", host.host_id] as const;
  const inventoryQuery = useQuery({
    queryKey,
    queryFn: ({ signal }) => fetchSetupInventory(host.host_id, signal),
    retry: false,
  });
  const actionMutation = useMutation({
    mutationFn: (action: SetupAction) => runSetupAction(host.host_id, action),
  });
  const detectionMutation = useMutation({
    mutationFn: (request?: SetupDetectRequest) => detectSetup(host.host_id, request),
  });
  const statusMutation = useMutation({
    mutationFn: (harness: SetupStatusHarness) => detectSetup(host.host_id, { harness }),
  });
  const piDefaultMutation = useMutation({
    mutationFn: () => detectSetup(host.host_id, { pi_default: true }),
  });
  const operationMutation = useMutation({
    mutationFn: ({
      action,
      parameters,
    }: {
      action: SetupOperationAction;
      parameters?: Record<string, unknown>;
    }) => startSetupOperation(host.host_id, action, parameters),
  });
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [detection, setDetection] = useState<SetupDetection | null>(null);
  const [detectionRequest, setDetectionRequest] = useState<SetupDetectRequest | null>(null);
  const [piDefaultResult, setPiDefaultResult] = useState<{
    inventory: SetupInventory;
    provider: string | null;
  } | null>(null);
  const [checkedStatuses, setCheckedStatuses] = useState<
    Partial<Record<SetupStatusHarness, SetupHarnessStatus>>
  >({});
  const [statusErrors, setStatusErrors] = useState<Partial<Record<SetupStatusHarness, string>>>({});
  const [checkingHarness, setCheckingHarness] = useState<SetupStatusHarness | null>(null);
  const [operation, setOperation] = useState<SetupOperation | null>(null);
  const currentOperationIdRef = useRef<string | null>(null);
  const latestOperationRef = useRef<SetupOperation | null>(null);
  const [operationAgentId, setOperationAgentId] = useState<string | null>(null);
  const [operationRecoveryPending, setOperationRecoveryPending] = useState(
    () => readSetupOperationId(host.host_id) !== null,
  );
  const [operationRecoveryError, setOperationRecoveryError] = useState<string | null>(null);
  const [operationRecoveryAttempt, setOperationRecoveryAttempt] = useState(0);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const operationPanelRef = useRef<HTMLDivElement | null>(null);
  const operationToRevealRef = useRef<string | null>(null);

  const savedInventory = inventoryQuery.data;
  const piDefaultChecked = !!piDefaultResult && piDefaultResult.inventory === savedInventory;
  const inventory =
    savedInventory && piDefaultChecked
      ? {
          ...savedInventory,
          effective_defaults: {
            ...savedInventory.effective_defaults,
            pi: piDefaultResult.provider,
          },
        }
      : savedInventory;
  const serverGate = isFeatureEnabled(info, "harness_install");
  const canMutate = !!inventory && inventory.mutations_enabled !== false && serverGate;
  const operationActive = operation?.state === "pending" || operation?.state === "running";
  const operationId = operation?.operation_id;
  const busy =
    actionMutation.isPending ||
    operationMutation.isPending ||
    statusMutation.isPending ||
    piDefaultMutation.isPending ||
    operationActive ||
    operationRecoveryPending ||
    operationRecoveryError !== null;

  useEffect(() => {
    const rememberedOperationId = readSetupOperationId(host.host_id);
    if (!rememberedOperationId) {
      setOperationRecoveryPending(false);
      setOperationRecoveryError(null);
      return;
    }
    const controller = new AbortController();
    let disposed = false;
    setOperationRecoveryPending(true);
    setOperationRecoveryError(null);
    void fetchSetupOperation(host.host_id, rememberedOperationId, controller.signal)
      .then((recovered) => {
        if (disposed) return;
        if (recovered.state === "pending" || recovered.state === "running") {
          currentOperationIdRef.current = recovered.operation_id;
          latestOperationRef.current = recovered;
          setOperation(recovered);
          const recoveredAgentId =
            recovered.action === "databricks-configure"
              ? readSetupOperationAgent(host.host_id)
              : agentIdForOperation(recovered.action);
          setOperationAgentId(recoveredAgentId);
          setSelectedAgentId(recoveredAgentId);
        } else {
          forgetSetupOperation(host.host_id);
        }
      })
      .catch((reason: unknown) => {
        if (disposed || controller.signal.aborted) return;
        if (reason instanceof Error && "status" in reason && reason.status === 404) {
          forgetSetupOperation(host.host_id);
          return;
        }
        setOperationRecoveryError(
          reason instanceof Error ? reason.message : "The previous setup operation is unavailable.",
        );
      })
      .finally(() => {
        if (!disposed) setOperationRecoveryPending(false);
      });
    return () => {
      disposed = true;
      controller.abort();
    };
  }, [host.host_id, operationRecoveryAttempt]);

  useEffect(() => {
    if (!operationId || operationToRevealRef.current !== operationId) return;
    operationToRevealRef.current = null;
    operationPanelRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [operationId]);

  const updateOperation = (next: SetupOperation) => {
    if (next.operation_id !== currentOperationIdRef.current) return;
    const previous = latestOperationRef.current;
    const previousFinished =
      previous && previous.state !== "pending" && previous.state !== "running";
    const nextActive = next.state === "pending" || next.state === "running";
    if (previous?.operation_id === next.operation_id && previousFinished && nextActive) return;
    latestOperationRef.current = next;
    setOperation(next);
    if (next.state === "pending" || next.state === "running") {
      rememberSetupOperation(host.host_id, next.operation_id);
    } else {
      forgetSetupOperation(host.host_id);
    }
  };

  const act = async (action: SetupAction): Promise<boolean> => {
    if (operationActive || operationRecoveryPending || operationRecoveryError) return false;
    setCheckedStatuses({});
    setPiDefaultResult(null);
    setStatusErrors({});
    setError(null);
    setNotice(null);
    try {
      const result = await actionMutation.mutateAsync(action);
      queryClient.setQueryData(queryKey, result.inventory);
      void queryClient.invalidateQueries({ queryKey });
      void queryClient.invalidateQueries({ queryKey: ["hosts"] });
      setNotice(result.message);
      return true;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The setup change failed.");
      return false;
    }
  };

  const detect = async (request?: SetupDetectRequest) => {
    if (operationActive || operationRecoveryPending || operationRecoveryError) return;
    setError(null);
    setNotice(null);
    try {
      setDetection(await detectionMutation.mutateAsync(request));
      setDetectionRequest(request ?? null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Detection failed.");
    }
  };

  const checkPiDefault = async () => {
    if (busy || !savedInventory) return;
    setPiDefaultResult(null);
    setError(null);
    try {
      const result = await piDefaultMutation.mutateAsync();
      const provider = result.pi_default_provider ?? null;
      if (
        result.pi_default_checked &&
        (provider === null || savedInventory.providers.some((item) => item.name === provider))
      ) {
        setPiDefaultResult({ inventory: savedInventory, provider });
      } else {
        setError(result.warnings?.join(" ") || "Pi's default could not be checked.");
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Pi's default could not be checked.");
    }
  };

  const checkStatus = async (harness: SetupStatusHarness) => {
    if (busy) return;
    setCheckedStatuses((statuses) => ({ ...statuses, [harness]: undefined }));
    setStatusErrors((errors) => ({ ...errors, [harness]: undefined }));
    setCheckingHarness(harness);
    try {
      const result = await statusMutation.mutateAsync(harness);
      if (result.harness_status?.harness === harness) {
        setCheckedStatuses((statuses) => ({ ...statuses, [harness]: result.harness_status }));
        void inventoryQuery.refetch();
      } else {
        setStatusErrors((errors) => ({
          ...errors,
          [harness]:
            result.warnings?.join(" ") || "Setup status could not be checked on this computer.",
        }));
      }
    } catch (reason) {
      setStatusErrors((errors) => ({
        ...errors,
        [harness]: reason instanceof Error ? reason.message : "Setup status could not be checked.",
      }));
    } finally {
      setCheckingHarness(null);
    }
  };

  const start = async (
    action: SetupOperationAction,
    parameters: Record<string, unknown> = {},
    originatingAgent?: string,
  ): Promise<boolean> => {
    if (operationActive || operationRecoveryPending || operationRecoveryError) return false;
    setCheckedStatuses({});
    setPiDefaultResult(null);
    setStatusErrors({});
    setError(null);
    setNotice(null);
    try {
      const started = await operationMutation.mutateAsync({ action, parameters });
      operationToRevealRef.current = started.operation_id;
      currentOperationIdRef.current = started.operation_id;
      updateOperation(started);
      setOperationAgentId(originatingAgent ?? agentIdForOperation(started.action));
      if (started.action === "databricks-configure" && originatingAgent) {
        try {
          sessionStorage.setItem(setupOperationAgentKey(host.host_id), originatingAgent);
        } catch {
          // The active page still keeps the originating agent.
        }
      }
      setSelectedAgentId(originatingAgent ?? agentIdForOperation(started.action));
      return true;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The guided setup could not start.");
      return false;
    }
  };

  if (inventoryQuery.isPending) {
    return (
      <div className="flex items-center gap-2 rounded-xl border bg-card p-5 text-sm text-muted-foreground">
        <Loader2Icon className="size-4 animate-spin" /> Loading provider settings from {host.name}…
      </div>
    );
  }
  if (inventoryQuery.isError || !inventory) {
    return (
      <div className="rounded-xl border bg-card p-4">
        <InlineError
          message={
            inventoryQuery.error instanceof Error
              ? inventoryQuery.error.message
              : `Provider settings are unavailable on ${host.name}.`
          }
          onRetry={() => void inventoryQuery.refetch()}
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      {!canMutate && (
        <div className="rounded-xl border border-border bg-muted/40 p-4 text-sm text-muted-foreground">
          Provider setup changes are disabled on this server. You can still review the configuration
          reported by {host.name}.
        </div>
      )}
      {notice && (
        <div
          role="status"
          className="rounded-lg border border-success/40 bg-success/10 px-3 py-2 text-sm"
        >
          {notice}
        </div>
      )}
      {error && <InlineError message={error} />}
      {operationRecoveryPending && (
        <div
          role="status"
          className="flex items-center gap-2 rounded-lg border border-border bg-muted/30 px-3 py-2 text-sm text-muted-foreground"
        >
          <Loader2Icon className="size-4 animate-spin" aria-hidden="true" />
          Restoring setup operation…
        </div>
      )}
      {operationRecoveryError && (
        <InlineError
          message={`The previous setup operation could not be restored: ${operationRecoveryError}`}
          onRetry={() => setOperationRecoveryAttempt((attempt) => attempt + 1)}
        />
      )}
      {operationMutation.isPending && (
        <div
          role="status"
          className="flex items-center gap-2 rounded-lg border border-border bg-muted/30 px-3 py-2 text-sm text-muted-foreground"
        >
          <Loader2Icon className="size-4 animate-spin" aria-hidden="true" />
          {operationMutation.variables?.action === "antigravity-login"
            ? "Checking connection…"
            : "Starting setup…"}
        </div>
      )}
      {operation?.action === "databricks-configure" && !operationAgentId && (
        <div ref={operationPanelRef} className="rounded-xl border border-border bg-card p-4">
          <ProviderSetupTerminal
            key={operation.operation_id}
            hostId={host.host_id}
            operation={operation}
            title="Configure Databricks"
            onOperationChange={updateOperation}
            onFinished={() => {
              void inventoryQuery.refetch();
              void queryClient.invalidateQueries({ queryKey: ["hosts"] });
            }}
          />
        </div>
      )}
      <AgentSettings
        host={host}
        inventory={inventory}
        canMutate={canMutate}
        busy={busy}
        detection={detection}
        detecting={detectionMutation.isPending}
        onDetect={() => void detect()}
        piDefaultChecked={piDefaultChecked}
        onCheckPiDefault={() => void checkPiDefault()}
        checkedStatuses={checkedStatuses}
        statusErrors={statusErrors}
        checkingHarness={checkingHarness}
        onCheckStatus={(harness) => void checkStatus(harness)}
        onAction={act}
        onStart={start}
        selectedAgentId={selectedAgentId}
        onSelectAgent={setSelectedAgentId}
        operation={operation}
        operationAgentId={operationAgentId}
        operationPanelRef={operationPanelRef}
        onOperationChange={updateOperation}
        onOperationFinished={() => {
          void inventoryQuery.refetch();
          void queryClient.invalidateQueries({ queryKey: ["hosts"] });
        }}
      />
      <AdvancedProviderTools
        inventory={inventory}
        detection={detection}
        detectionRequest={detectionRequest}
        canMutate={canMutate}
        busy={busy}
        onAction={act}
        onDetectImport={(request) => void detect(request)}
        detecting={detectionMutation.isPending}
        onDetect={() => void detect()}
      />
    </div>
  );
}

function SectionCard({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <section className="flex flex-col gap-3">
      <div>
        <h2 className="text-lg font-semibold">{title}</h2>
        <p className="text-sm text-muted-foreground">{description}</p>
      </div>
      <div className="rounded-xl border border-border bg-card">{children}</div>
    </section>
  );
}

function StatusBadge({ children, good = false }: { children: React.ReactNode; good?: boolean }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-xs",
        good
          ? "border-success/35 bg-success/10 text-success-foreground"
          : "border-border bg-muted/60 text-muted-foreground",
      )}
    >
      {children}
    </span>
  );
}

function providerSurfaces(provider: SetupProvider): ProviderSurface[] {
  return provider.default_scopes.filter((surface): surface is ProviderSurface =>
    ["anthropic", "openai", "gemini", "pi"].includes(surface),
  );
}

const CLAUDE_KEYCHAIN_DETECTION_NOTICE =
  "Claude logins stored only in the OS Keychain are checked through guided sign-in, not detection.";

function primarySurfaceForAgent(agent: AgentOption): ProviderSurface | undefined {
  return agent.id === "pi" ? "pi" : agent.surfaces[0];
}

function providerMatchesAgent(provider: SetupProvider, agent: AgentOption): boolean {
  const surface = primarySurfaceForAgent(agent);
  return !!surface && providerSurfaces(provider).includes(surface);
}

function detectedProviderMatchesAgent(
  provider: SetupDetection["providers"][number],
  agent: AgentOption,
): boolean {
  if (agent.id === "pi") {
    if (provider.kind === "subscription") return provider.family === "pi";
    if (provider.kind === "bedrock") return false;
  }
  return agent.surfaces.includes(provider.family as ProviderSurface);
}

function ProviderOverview({
  inventory,
  canMutate,
  busy,
  detection,
  detecting,
  onDetect,
  onAction,
}: {
  inventory: SetupInventory;
  canMutate: boolean;
  busy: boolean;
  detection: SetupDetection | null;
  detecting: boolean;
  onDetect: () => void;
  onAction: (action: SetupAction) => Promise<boolean>;
}) {
  const [removing, setRemoving] = useState<SetupProvider | null>(null);
  return (
    <SectionCard
      title="Connections"
      description="Named credentials and gateways available to new agent processes."
    >
      <div className="flex flex-wrap items-center justify-between gap-3 border-b p-4">
        <p className="text-sm text-muted-foreground">
          This overview reads local configuration only. “Configured locally” means a provider entry
          is saved; it does not verify a vendor account or token. Detection runs only when you ask
          for it.
        </p>
        {canMutate && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            loading={detecting}
            disabled={busy}
            data-testid="settings-provider-detect"
            onClick={onDetect}
          >
            <WandSparklesIcon className="size-4" /> Detect credentials
          </Button>
        )}
      </div>
      {inventory.providers.length === 0 ? (
        <p className="p-4 text-sm text-muted-foreground">No providers are configured.</p>
      ) : (
        <div className="divide-y">
          {inventory.providers.map((provider) => (
            <ProviderRow
              key={provider.name}
              provider={provider}
              effectiveDefaults={inventory.effective_defaults}
              canMutate={canMutate}
              busy={busy}
              onDefault={(surface) =>
                void onAction({ action: "set_default", name: provider.name, surface })
              }
              onRemove={() => setRemoving(provider)}
            />
          ))}
        </div>
      )}
      {detection && (
        <DetectionResults
          detection={detection}
          dismissed={inventory.dismissed_detections}
          canMutate={canMutate}
          busy={busy}
          onAction={onAction}
        />
      )}
      {removing && (
        <ConfirmRemoval
          provider={removing}
          busy={busy}
          onCancel={() => setRemoving(null)}
          onConfirm={async () => {
            if (await onAction({ action: "remove_provider", name: removing.name })) {
              setRemoving(null);
            }
          }}
        />
      )}
    </SectionCard>
  );
}

function ProviderRow({
  provider,
  effectiveDefaults,
  canMutate,
  busy,
  onDefault,
  onRemove,
}: {
  provider: SetupProvider;
  effectiveDefaults: Record<string, string | null>;
  canMutate: boolean;
  busy: boolean;
  onDefault: (surface: ProviderSurface) => void;
  onRemove: () => void;
}) {
  const surfaces = providerSurfaces(provider);
  const [selectedSurface, setSurface] = useState<ProviderSurface>(surfaces[0] ?? "openai");
  const surface = surfaces.includes(selectedSurface) ? selectedSurface : surfaces[0];
  const effective = Object.entries(effectiveDefaults)
    .filter(([, name]) => name === provider.name)
    .map(([scope]) => scope);
  return (
    <div className="flex flex-col gap-3 p-4" data-testid={`provider-row-${provider.name}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{provider.name}</span>
            <StatusBadge good>Configured locally</StatusBadge>
            <StatusBadge>{provider.kind.replaceAll("_", " ")}</StatusBadge>
            {provider.defaults.map((item) => (
              <StatusBadge key={item} good>
                Default for {setupLabel(item)}
              </StatusBadge>
            ))}
            {effective
              .filter((item) => !provider.defaults.includes(item))
              .map((item) => (
                <StatusBadge key={`effective-${item}`}>
                  Effective for {setupLabel(item)}
                </StatusBadge>
              ))}
          </div>
          <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted-foreground">
            {provider.families.length > 0 && (
              <span>{provider.families.map(setupLabel).join(" + ")}</span>
            )}
            {Object.entries(provider.credential_sources).map(([family, source]) => (
              <span key={`credential-${family}`}>
                {setupLabel(family)} credential: {setupLabel(source)}
              </span>
            ))}
            {Object.entries(provider.models).map(([family, model]) => (
              <span key={family}>
                {setupLabel(family)}: {model}
              </span>
            ))}
            {Object.values(provider.base_urls).map((url) => (
              <span key={url} className="max-w-full truncate">
                {url}
              </span>
            ))}
          </div>
        </div>
        {canMutate && (
          <div className="flex flex-wrap items-center gap-2">
            {surfaces.length > 0 && (
              <>
                <Select
                  value={surface}
                  onValueChange={(next) => setSurface(next as ProviderSurface)}
                >
                  <SelectTrigger
                    aria-label={`Default scope for ${provider.name}`}
                    className="h-8 w-32"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {surfaces.map((item) => (
                      <SelectItem key={item} value={item}>
                        {setupLabel(item)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={busy}
                  onClick={() => onDefault(surface)}
                >
                  Make default
                </Button>
              </>
            )}
            <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={onRemove}>
              <Trash2Icon className="size-4" /> Remove
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}

function ConfirmRemoval({
  provider,
  busy,
  onCancel,
  onConfirm,
}: {
  provider: SetupProvider;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div
      className="border-t border-destructive/30 bg-destructive/5 p-4"
      role="alertdialog"
      aria-label={`Remove ${provider.name}`}
    >
      <div className="flex items-start gap-2">
        <AlertTriangleIcon className="mt-0.5 size-4 shrink-0 text-destructive" />
        <div className="flex-1">
          <p className="text-sm font-medium">Remove {provider.name}?</p>
          <p className="mt-1 text-sm text-muted-foreground">
            {provider.remove_warning ??
              "New processes will no longer use this connection. Running processes keep their current environment until they exit."}
          </p>
          <div className="mt-3 flex gap-2">
            <Button
              type="button"
              size="sm"
              variant="destructive"
              loading={busy}
              onClick={onConfirm}
            >
              Remove provider
            </Button>
            <Button type="button" size="sm" variant="outline" disabled={busy} onClick={onCancel}>
              Cancel
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

function DetectionResults({
  detection,
  dismissed,
  canMutate,
  busy,
  onAction,
}: {
  detection: SetupDetection;
  dismissed: string[];
  canMutate: boolean;
  busy: boolean;
  onAction: (action: SetupAction) => Promise<boolean>;
}) {
  return (
    <div className="border-t bg-muted/20 p-4" data-testid="provider-detection-results">
      <h3 className="text-sm font-medium">Detected on this computer</h3>
      {detection.warnings?.map((warning) => (
        <p key={warning} className="mt-2 text-sm text-amber-700 dark:text-amber-400">
          {warning}
        </p>
      ))}
      {detection.providers.length === 0 ? (
        <p className="mt-2 text-sm text-muted-foreground">No additional credentials were found.</p>
      ) : (
        <div className="mt-2 divide-y rounded-lg border bg-card">
          {detection.providers.map((provider) => {
            const ignored = dismissed.includes(provider.name);
            return (
              <div
                key={provider.name}
                className="flex flex-wrap items-center justify-between gap-3 p-3"
              >
                <div>
                  <div className="text-sm font-medium">
                    {provider.display_name ?? provider.name}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {setupLabel(provider.family)} · {setupLabel(provider.source)}
                  </div>
                </div>
                {canMutate && (
                  <div className="flex gap-2">
                    {ignored ? (
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={busy}
                        onClick={() =>
                          void onAction({
                            action: "dismiss_detection",
                            name: provider.name,
                            dismissed: false,
                          })
                        }
                      >
                        Show again
                      </Button>
                    ) : (
                      <>
                        <Button
                          type="button"
                          size="sm"
                          disabled={busy}
                          onClick={() =>
                            void onAction({ action: "adopt_detected", name: provider.name })
                          }
                        >
                          Use credential
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
                          disabled={busy}
                          onClick={() =>
                            void onAction({
                              action: "dismiss_detection",
                              name: provider.name,
                              dismissed: true,
                            })
                          }
                        >
                          Ignore
                        </Button>
                      </>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ProviderForms({
  inventory,
  detection,
  canMutate,
  busy,
  onAction,
}: {
  inventory: SetupInventory;
  detection: SetupDetection | null;
  canMutate: boolean;
  busy: boolean;
  onAction: (action: SetupAction) => Promise<boolean>;
}) {
  const [open, setOpen] = useState<"key" | "gateway" | "bedrock" | null>(null);
  if (!canMutate) return null;
  return (
    <SectionCard
      title="Add a connection"
      description="Keys stay in the host credential store; saved settings only contain a reference."
    >
      <div className="flex flex-wrap gap-2 p-4">
        <Button
          type="button"
          variant={open === "key" ? "secondary" : "outline"}
          disabled={busy}
          onClick={() => setOpen(open === "key" ? null : "key")}
        >
          <KeyRoundIcon className="size-4" /> Add provider
        </Button>
        <Button
          type="button"
          variant={open === "gateway" ? "secondary" : "outline"}
          disabled={busy}
          onClick={() => setOpen(open === "gateway" ? null : "gateway")}
        >
          <CloudIcon className="size-4" /> Add gateway
        </Button>
        <Button
          type="button"
          variant={open === "bedrock" ? "secondary" : "outline"}
          disabled={busy}
          onClick={() => setOpen(open === "bedrock" ? null : "bedrock")}
        >
          <ServerCogIcon className="size-4" /> Add Bedrock
        </Button>
      </div>
      {open && (
        <div className="border-t p-4">
          {open === "key" && (
            <KeyProviderForm
              catalog={inventory.key_providers}
              defaultModels={detection?.default_models ?? {}}
              busy={busy}
              onAction={onAction}
              onDone={() => setOpen(null)}
            />
          )}
          {open === "gateway" && (
            <GatewayForm busy={busy} onAction={onAction} onDone={() => setOpen(null)} />
          )}
          {open === "bedrock" && (
            <BedrockForm busy={busy} onAction={onAction} onDone={() => setOpen(null)} />
          )}
        </div>
      )}
    </SectionCard>
  );
}

function SecretOrEnvironment({
  secret,
  envVar,
  onSecret,
  onEnvVar,
}: {
  secret: string;
  envVar: string;
  onSecret: (value: string) => void;
  onEnvVar: (value: string) => void;
}) {
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <label className="flex flex-col gap-1 text-sm font-medium">
        API key or token
        <Input
          type="password"
          autoComplete="off"
          value={secret}
          onChange={(event) => onSecret(event.target.value)}
          placeholder="Stored securely"
        />
      </label>
      <label className="flex flex-col gap-1 text-sm font-medium">
        Environment variable
        <Input
          value={envVar}
          onChange={(event) => onEnvVar(event.target.value)}
          placeholder="Or use OPENAI_API_KEY"
        />
      </label>
    </div>
  );
}

function credentialPayload(secret: string, envVar: string): { secret?: string; env_var?: string } {
  return envVar.trim() ? { env_var: envVar.trim() } : { secret: secret.trim() };
}

function KeyProviderForm({
  catalog,
  defaultModels,
  busy,
  onAction,
  onDone,
}: {
  catalog: SetupKeyProvider[];
  defaultModels: Record<string, string | null>;
  busy: boolean;
  onAction: (action: SetupAction) => Promise<boolean>;
  onDone: () => void;
}) {
  const [provider, setProvider] = useState(catalog[0]?.id ?? "");
  const [name, setName] = useState("");
  const [model, setModel] = useState(defaultModels[catalog[0]?.id ?? ""] ?? "");
  const [secret, setSecret] = useState("");
  const [envVar, setEnvVar] = useState("");
  const [more, setMore] = useState(false);
  // Model discovery is opt-in because it can inspect local configuration. When
  // the user has explicitly checked and the catalog has no default, require a
  // model here instead of failing after the credential is submitted.
  const requiresModel = Object.hasOwn(defaultModels, provider) && !defaultModels[provider];
  const valid = provider && (secret.trim() || envVar.trim()) && (!requiresModel || model.trim());
  if (catalog.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        This computer does not offer a compatible API-key provider.
      </p>
    );
  }
  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={async (event) => {
        event.preventDefault();
        if (!valid) return;
        if (
          await onAction({
            action: "add_key",
            provider,
            name: name.trim() || undefined,
            ...(model.trim() ? { model: model.trim() } : {}),
            ...credentialPayload(secret, envVar),
          })
        ) {
          setSecret("");
          setEnvVar("");
          onDone();
        }
      }}
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="flex flex-col gap-1 text-sm font-medium">
          Vendor
          <Select
            value={provider}
            onValueChange={(next) => {
              setProvider(next);
              setModel(defaultModels[next] ?? "");
            }}
          >
            <SelectTrigger aria-label="Vendor">
              <SelectValue placeholder="Choose vendor" />
            </SelectTrigger>
            <SelectContent>
              {catalog.map((item) => (
                <SelectItem key={item.id} value={item.id}>
                  {item.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>
        <label className="flex flex-col gap-1 text-sm font-medium">
          API key
          <Input
            type="password"
            autoComplete="off"
            value={secret}
            onChange={(event) => setSecret(event.target.value)}
            placeholder="Stored securely on this computer"
          />
        </label>
      </div>
      {requiresModel && (
        <label className="flex flex-col gap-1 text-sm font-medium">
          Default model
          <Input
            required
            value={model}
            onChange={(event) => setModel(event.target.value)}
            placeholder="Required for this vendor"
          />
          <span className="text-xs font-normal text-muted-foreground">
            This vendor has no catalog default on this computer.
          </span>
        </label>
      )}
      <button
        type="button"
        className="self-start text-sm text-muted-foreground hover:text-foreground"
        onClick={() => setMore(!more)}
        aria-expanded={more}
      >
        More options
      </button>
      {more && (
        <div className="grid gap-3 rounded-lg border bg-muted/20 p-3 sm:grid-cols-3">
          <label className="flex flex-col gap-1 text-sm font-medium">
            Connection name
            <Input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Optional"
            />
          </label>
          {!requiresModel && (
            <label className="flex flex-col gap-1 text-sm font-medium">
              Default model
              <Input
                value={model}
                onChange={(event) => setModel(event.target.value)}
                placeholder="Optional — uses the catalog default when blank"
              />
            </label>
          )}
          <label className="flex flex-col gap-1 text-sm font-medium">
            Environment variable instead of key
            <Input
              value={envVar}
              onChange={(event) => setEnvVar(event.target.value)}
              placeholder="OPENAI_API_KEY"
            />
          </label>
        </div>
      )}
      <div>
        <Button type="submit" loading={busy} disabled={!valid}>
          Save provider
        </Button>
      </div>
    </form>
  );
}

function GatewayForm({
  initialFamily,
  busy,
  onAction,
  onDone,
}: {
  initialFamily?: "anthropic" | "openai";
  busy: boolean;
  onAction: (action: SetupAction) => Promise<boolean>;
  onDone: () => void;
}) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [secret, setSecret] = useState("");
  const [envVar, setEnvVar] = useState("");
  const [anthropic, setAnthropic] = useState(initialFamily !== "openai");
  const [openai, setOpenai] = useState(initialFamily !== "anthropic");
  const [wire, setWire] = useState<"chat" | "responses">("responses");
  const [anthropicModel, setAnthropicModel] = useState("");
  const [openaiModel, setOpenaiModel] = useState("");
  const valid =
    name.trim() &&
    url.trim() &&
    (anthropic || openai) &&
    (!anthropic || anthropicModel.trim()) &&
    (!openai || openaiModel.trim()) &&
    (secret.trim() || envVar.trim());
  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={async (event) => {
        event.preventDefault();
        if (!valid) return;
        const models: Record<string, string> = {};
        if (anthropicModel.trim()) models.anthropic = anthropicModel.trim();
        if (openaiModel.trim()) models.openai = openaiModel.trim();
        if (
          await onAction({
            action: "add_gateway",
            name: name.trim(),
            base_url: url.trim(),
            families: [anthropic ? "anthropic" : null, openai ? "openai" : null].filter(
              (item): item is "anthropic" | "openai" => item !== null,
            ),
            wire_api: wire,
            models,
            ...credentialPayload(secret, envVar),
          })
        ) {
          setSecret("");
          setEnvVar("");
          onDone();
        }
      }}
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="flex flex-col gap-1 text-sm font-medium">
          Gateway name
          <Input value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1 text-sm font-medium">
          Base URL
          <Input
            inputMode="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://gateway.example.com"
          />
        </label>
      </div>
      <div className="flex flex-wrap gap-5 text-sm">
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={anthropic}
            onChange={(e) => setAnthropic(e.target.checked)}
          />{" "}
          Anthropic family
        </label>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={openai} onChange={(e) => setOpenai(e.target.checked)} />{" "}
          OpenAI family
        </label>
        <label className="flex items-center gap-2">
          OpenAI protocol
          <Select value={wire} onValueChange={(v) => setWire(v as "chat" | "responses")}>
            <SelectTrigger className="h-8 w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="responses">Responses</SelectItem>
              <SelectItem value="chat">Chat</SelectItem>
            </SelectContent>
          </Select>
        </label>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="flex flex-col gap-1 text-sm font-medium">
          Anthropic model
          <Input
            disabled={!anthropic}
            value={anthropicModel}
            onChange={(e) => setAnthropicModel(e.target.value)}
            placeholder="Model ID"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm font-medium">
          OpenAI model
          <Input
            disabled={!openai}
            value={openaiModel}
            onChange={(e) => setOpenaiModel(e.target.value)}
            placeholder="Model ID"
          />
        </label>
      </div>
      <SecretOrEnvironment
        secret={secret}
        envVar={envVar}
        onSecret={setSecret}
        onEnvVar={setEnvVar}
      />
      <div>
        <Button type="submit" loading={busy} disabled={!valid}>
          Save gateway
        </Button>
      </div>
    </form>
  );
}

function BedrockForm({
  busy,
  onAction,
  onDone,
}: {
  busy: boolean;
  onAction: (action: SetupAction) => Promise<boolean>;
  onDone: () => void;
}) {
  const [name, setName] = useState("bedrock");
  const [url, setUrl] = useState("https://bedrock-runtime.us-east-1.amazonaws.com");
  const [model, setModel] = useState("");
  const [secret, setSecret] = useState("");
  const [envVar, setEnvVar] = useState("");
  const valid = name.trim() && url.trim() && model.trim() && (secret.trim() || envVar.trim());
  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={async (event) => {
        event.preventDefault();
        if (!valid) return;
        if (
          await onAction({
            action: "add_bedrock",
            name: name.trim(),
            base_url: url.trim(),
            model: model.trim(),
            ...credentialPayload(secret, envVar),
          })
        ) {
          setSecret("");
          setEnvVar("");
          onDone();
        }
      }}
    >
      <div className="grid gap-3 sm:grid-cols-3">
        <label className="flex flex-col gap-1 text-sm font-medium">
          Connection name
          <Input value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1 text-sm font-medium">
          Bedrock endpoint
          <Input inputMode="url" value={url} onChange={(e) => setUrl(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1 text-sm font-medium">
          Model ID
          <Input value={model} onChange={(e) => setModel(e.target.value)} />
        </label>
      </div>
      <SecretOrEnvironment
        secret={secret}
        envVar={envVar}
        onSecret={setSecret}
        onEnvVar={setEnvVar}
      />
      <div>
        <Button type="submit" loading={busy} disabled={!valid}>
          Save Bedrock
        </Button>
      </div>
    </form>
  );
}

const GUIDED_HARNESSES: {
  id: SetupStatusHarness;
  label: string;
  action?: SetupOperationAction;
  logout?: SetupOperationAction;
}[] = [
  { id: "claude-native", label: "Claude Code", action: "claude-login" },
  { id: "codex-native", label: "Codex", action: "codex-login" },
  { id: "pi-native", label: "Pi" },
  { id: "cursor", label: "Cursor", action: "cursor-login", logout: "cursor-logout" },
  { id: "antigravity", label: "Antigravity", action: "antigravity-login" },
  { id: "copilot", label: "GitHub Copilot" },
  { id: "opencode", label: "OpenCode", action: "opencode-login" },
  { id: "qwen", label: "Qwen", action: "qwen-configure" },
  { id: "goose", label: "Goose", action: "goose-configure" },
  { id: "hermes", label: "Hermes", action: "hermes-configure" },
  { id: "kiro", label: "Kiro", action: "kiro-login" },
  { id: "kimi", label: "Kimi", action: "kimi-login" },
];

function operationAvailable(inventory: SetupInventory, action: SetupOperationAction): boolean {
  return inventory.supported_operations.includes(action);
}

interface AgentOption {
  id: string;
  label: string;
  harness: SetupStatusHarness;
  surfaces: ProviderSurface[];
  login?: SetupOperationAction;
  logout?: SetupOperationAction;
  icon: React.ComponentType<{ className?: string }>;
}

const AGENT_OPTIONS: AgentOption[] = [
  {
    id: "claude",
    label: "Claude Code",
    harness: "claude-native",
    surfaces: ["anthropic"],
    login: "claude-login",
    icon: ClaudeIcon,
  },
  {
    id: "codex",
    label: "Codex",
    harness: "codex-native",
    surfaces: ["openai"],
    login: "codex-login",
    icon: CodexIcon,
  },
  {
    id: "cursor",
    label: "Cursor",
    harness: "cursor",
    surfaces: [],
    login: "cursor-login",
    logout: "cursor-logout",
    icon: CursorIcon,
  },
  {
    id: "opencode",
    label: "OpenCode",
    harness: "opencode",
    surfaces: [],
    login: "opencode-login",
    icon: OpenCodeIcon,
  },
  {
    id: "pi",
    label: "Pi",
    harness: "pi-native",
    surfaces: ["pi", "anthropic", "openai"],
    icon: PiIcon,
  },
  ...GUIDED_HARNESSES.filter(
    (item) =>
      !["claude-native", "codex-native", "cursor", "opencode", "pi-native"].includes(item.id),
  ).map((item) => ({
    id: item.id,
    label: item.label,
    harness: item.id === "antigravity" ? "antigravity-native" : item.id,
    surfaces: [],
    login: item.action,
    logout: item.logout,
    icon: iconForAgent({ name: item.id, harness: item.id }),
  })),
];

function agentIdForOperation(action: SetupOperationAction): string {
  if (action.startsWith("claude-")) return "claude";
  if (action.startsWith("codex-") || action === "databricks-configure") return "codex";
  return action.split("-")[0];
}

type AgentMethod =
  | "key"
  | "gateway"
  | "bedrock"
  | "databricks"
  | "cursor-key"
  | "antigravity-key"
  | "copilot-settings"
  | "opencode-model"
  | null;

function checkedSetupStatusLabel(availability: SetupHarnessStatus["availability"]): string {
  if (availability === true) return "Ready according to setup";
  if (availability === "needs-auth") return "Sign-in or configuration needed";
  if (availability === "version-too-low") return "Update needed";
  return "Installation needed";
}

function AgentSettings({
  host,
  inventory,
  canMutate,
  busy,
  detection,
  detecting,
  onDetect,
  piDefaultChecked,
  onCheckPiDefault,
  checkedStatuses,
  statusErrors,
  checkingHarness,
  onCheckStatus,
  onAction,
  onStart,
  selectedAgentId,
  onSelectAgent,
  operation,
  operationAgentId,
  operationPanelRef,
  onOperationChange,
  onOperationFinished,
}: {
  host: Host;
  inventory: SetupInventory;
  canMutate: boolean;
  busy: boolean;
  detection: SetupDetection | null;
  detecting: boolean;
  onDetect: () => void;
  piDefaultChecked: boolean;
  onCheckPiDefault: () => void;
  checkedStatuses: Partial<Record<SetupStatusHarness, SetupHarnessStatus>>;
  statusErrors: Partial<Record<SetupStatusHarness, string>>;
  checkingHarness: SetupStatusHarness | null;
  onCheckStatus: (harness: SetupStatusHarness) => void;
  onAction: (action: SetupAction) => Promise<boolean>;
  onStart: (
    action: SetupOperationAction,
    parameters?: Record<string, unknown>,
    originatingAgent?: string,
  ) => Promise<boolean>;
  selectedAgentId: string | null;
  onSelectAgent: (id: string | null) => void;
  operation: SetupOperation | null;
  operationAgentId: string | null;
  operationPanelRef: React.RefObject<HTMLDivElement | null>;
  onOperationChange: (operation: SetupOperation) => void;
  onOperationFinished: () => void;
}) {
  const [showMore, setShowMore] = useState(false);
  const [method, setMethod] = useState<AgentMethod>(null);
  const [managedProvider, setManagedProvider] = useState<string | null>(null);
  const setupSteps = useHarnessSetupSteps();
  const queryClient = useQueryClient();
  const install = useInstallHarness(host.host_id);
  const info = useServerInfo();
  const agent = AGENT_OPTIONS.find((item) => item.id === selectedAgentId) ?? null;
  const builtinAcp = inventory.builtin_acp?.find((item) => item.id === selectedAgentId);
  const compatible = agent
    ? inventory.providers.filter((provider) => providerMatchesAgent(provider, agent))
    : [];
  const primarySurface = agent ? primarySurfaceForAgent(agent) : undefined;
  const steps = agent ? resolveSetupSteps(setupSteps[agent.harness], agent.harness, host) : [];
  const installStep = steps.find((step) => step.kind === "install" && step.status === "todo");
  const readiness = agent
    ? (checkedStatuses[agent.harness]?.availability ?? host.configured_harnesses?.[agent.harness])
    : undefined;
  const availableLogin = !!agent?.login && operationAvailable(inventory, agent.login);
  const needsUpdate = readiness === "version-too-low";
  const needsInstall = !availableLogin && (readiness === false || readiness === "binary-missing");
  const canInstall = agent && harnessInstallableOnHost(info, agent.harness, host);
  const operationInProgress = operation?.state === "pending" || operation?.state === "running";
  const activeOperationAgent = AGENT_OPTIONS.find((item) => item.id === operationAgentId);
  const chooseAgent = (id: string | null) => {
    setMethod(null);
    setManagedProvider(null);
    onSelectAgent(id);
  };
  const agentStatus = (item: AgentOption) => {
    if (statusErrors[item.harness]) return "Status check failed";
    const checked = checkedStatuses[item.harness];
    if (checked) return checkedSetupStatusLabel(checked.availability);
    if (
      operationAgentId === item.id &&
      operation?.action === "antigravity-login" &&
      operation.state === "succeeded"
    ) {
      return "Connected";
    }
    const itemReadiness = host.configured_harnesses?.[item.harness];
    if (itemReadiness === true) return "Available on this computer";
    if (itemReadiness === "version-too-low") return "Update needed";
    if (
      (itemReadiness === false || itemReadiness === "binary-missing") &&
      !(item.login && operationAvailable(inventory, item.login))
    )
      return "Installation needed";
    if (itemReadiness === "needs-auth") return "Sign-in needed";
    const count = inventory.providers.filter((provider) =>
      providerMatchesAgent(provider, item),
    ).length;
    if (count) return `${count} saved connection${count === 1 ? "" : "s"}`;
    if (item.login && itemReadiness === undefined) return "Sign-in status not checked";
    if (item.login && operationAvailable(inventory, item.login)) return "Sign-in available";
    return "Choose how to connect";
  };

  const activeOperationNotice =
    operationInProgress && activeOperationAgent ? (
      <div
        role="status"
        className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-info/30 bg-info/5 px-4 py-3 text-sm"
      >
        <span>{activeOperationAgent.label} connection in progress.</span>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => chooseAgent(activeOperationAgent.id)}
        >
          Return to {activeOperationAgent.label}
        </Button>
      </div>
    ) : null;

  if (!agent && !builtinAcp) {
    return (
      <div className="flex flex-col gap-3">
        {activeOperationNotice}
        <section className="overflow-hidden rounded-xl border border-border bg-card">
          <div className="border-b px-4 py-3">
            <h2 className="text-base font-semibold">Agents</h2>
            <p className="text-sm text-muted-foreground">Choose an agent to connect.</p>
          </div>
          <div className="divide-y">
            {AGENT_OPTIONS.filter((_, index) => showMore || index < 5).map((item) => {
              const Icon = item.icon;
              return (
                <button
                  key={item.id}
                  data-testid={`setup-agent-${item.id}`}
                  type="button"
                  className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-muted/40"
                  onClick={() => chooseAgent(item.id)}
                >
                  <Icon className="size-5 shrink-0" />
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm font-medium">{item.label}</span>
                    <span className="block text-xs text-muted-foreground">{agentStatus(item)}</span>
                  </span>
                  <ChevronRightIcon className="size-4 text-muted-foreground" />
                </button>
              );
            })}
            {showMore &&
              inventory.builtin_acp?.map((item) => {
                const Icon = iconForAgent({ name: item.id, harness: item.id });
                return (
                  <button
                    key={item.id}
                    type="button"
                    data-testid={`setup-agent-${item.id}`}
                    className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-muted/40"
                    onClick={() => chooseAgent(item.id)}
                  >
                    <Icon className="size-5 shrink-0" />
                    <span className="min-w-0 flex-1 text-sm font-medium">{item.label}</span>
                    <ChevronRightIcon className="size-4 text-muted-foreground" />
                  </button>
                );
              })}
          </div>
          <button
            type="button"
            className="flex w-full items-center justify-between border-t px-4 py-3 text-left text-sm text-muted-foreground hover:bg-muted/40"
            onClick={() => setShowMore(!showMore)}
            aria-expanded={showMore}
          >
            {showMore ? "Fewer agents" : "More agents"}
            <ChevronDownIcon
              className={cn("size-4 transition-transform", showMore && "rotate-180")}
            />
          </button>
        </section>
      </div>
    );
  }

  if (builtinAcp) {
    const Icon = iconForAgent({ name: builtinAcp.id, harness: builtinAcp.id });
    return (
      <section className="rounded-xl border border-border bg-card p-4">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="-ml-2"
          onClick={() => chooseAgent(null)}
        >
          <ArrowLeftIcon className="size-4" /> Back to agents
        </Button>
        <div className="mt-3 flex items-center gap-3">
          <Icon className="size-6" />
          <h2 className="text-lg font-semibold">{builtinAcp.label}</h2>
        </div>
        <div className="mt-4 space-y-3 text-sm">
          <div>
            <h3 className="font-medium">Install on {host.name}</h3>
            <p className="mt-1 text-muted-foreground">{builtinAcp.install_command}</p>
          </div>
          <div>
            <h3 className="font-medium">Authenticate</h3>
            <p className="mt-1 text-muted-foreground">{builtinAcp.auth_instructions}</p>
          </div>
        </div>
      </section>
    );
  }

  if (!agent) return null;

  const Icon = agent.icon;
  const statusError = statusErrors[agent.harness];
  return (
    <div className="flex flex-col gap-3">
      {operationInProgress && operationAgentId !== agent.id && activeOperationNotice}
      <section className="overflow-hidden rounded-xl border border-border bg-card">
        <div className="border-b px-4 py-3">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="-ml-2 mb-2"
            onClick={() => chooseAgent(null)}
          >
            <ArrowLeftIcon className="size-4" /> Back to agents
          </Button>
          <div className="flex items-center gap-3">
            <Icon className="size-6" />
            <div>
              <h2 className="text-lg font-semibold">{agent.label}</h2>
              <p className="text-sm text-muted-foreground">{agentStatus(agent)}</p>
            </div>
          </div>
          {canMutate && (
            <div className="mt-3 flex flex-wrap items-center gap-3">
              <Button
                type="button"
                size="sm"
                variant="outline"
                loading={checkingHarness === agent.harness}
                disabled={busy}
                onClick={() => onCheckStatus(agent.harness)}
              >
                Check status
              </Button>
              <details className="text-xs text-muted-foreground">
                <summary className="cursor-pointer">About this check</summary>
                <p className="mt-1">
                  It reads local CLI setup and may request credential access. It does not verify a
                  vendor account.
                </p>
              </details>
            </div>
          )}
          {statusError && <InlineError message={statusError} />}
        </div>
        {(needsInstall || needsUpdate) && (
          <div className="flex flex-wrap items-center justify-between gap-3 border-b bg-muted/30 px-4 py-3">
            <div>
              <p className="text-sm font-medium">
                {needsUpdate ? `Update ${agent.label}` : `Install ${agent.label}`}
              </p>
              <p className="text-xs text-muted-foreground">
                {needsUpdate
                  ? `Update ${agent.label} on ${host.name}, then check setup status again.`
                  : (installStep?.detail ??
                    `${agent.label} must be installed on ${host.name} before sign-in.`)}
                {!needsUpdate &&
                  !canInstall &&
                  " Install it on this computer, then check setup status again."}
              </p>
            </div>
            {!needsUpdate && canInstall && canMutate && (
              <Button
                type="button"
                size="sm"
                variant="outline"
                loading={install.isPending}
                disabled={busy}
                onClick={() =>
                  install.mutate(agent.harness, {
                    onSuccess: () => {
                      void queryClient.invalidateQueries({
                        queryKey: ["provider-setup", host.host_id],
                      });
                    },
                  })
                }
              >
                Install
              </Button>
            )}
            {install.isError && <InlineError message={install.error.message} />}
          </div>
        )}
        {operation && operationAgentId === agent.id && operation.already_connected === true && (
          <div role="status" className="border-b bg-success/5 px-4 py-3 text-sm text-success">
            Connected. Antigravity is already signed in on this computer.
          </div>
        )}
        {operation &&
          operationAgentId === agent.id &&
          operation.action === "antigravity-login" &&
          operation.state === "succeeded" &&
          operation.already_connected !== true && (
            <div role="status" className="border-b bg-success/5 px-4 py-3 text-sm text-success">
              Connected.
            </div>
          )}
        {operation && operationAgentId === agent.id && operation.already_connected !== true && (
          <div ref={operationPanelRef} className="border-b p-4">
            <ProviderSetupTerminal
              key={operation.operation_id}
              hostId={host.host_id}
              operation={operation}
              title={
                operation.action === "codex-login"
                  ? "Sign in to ChatGPT"
                  : operation.action === "claude-login"
                    ? "Sign in to Claude"
                    : `${operation.action.replaceAll("-", " ")}`
              }
              onOperationChange={onOperationChange}
              onFinished={onOperationFinished}
            />
          </div>
        )}
        {agent.id === "pi" && inventory.pi_default_requires_detection && (
          <div className="flex flex-wrap items-center justify-between gap-3 border-b p-4 text-sm">
            <p className="text-muted-foreground">
              {piDefaultChecked
                ? inventory.effective_defaults.pi
                  ? `Default: ${inventory.effective_defaults.pi}`
                  : "No compatible default found."
                : "Check local CLI configuration to identify Pi’s default."}
            </p>
            {canMutate && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={onCheckPiDefault}
              >
                {piDefaultChecked ? "Check again" : "Check Pi default"}
              </Button>
            )}
          </div>
        )}
        {compatible.length > 0 && (
          <div className="border-b">
            <h3 className="px-4 pt-4 text-sm font-medium">Saved connections</h3>
            <p className="px-4 pb-2 text-xs text-muted-foreground">
              Changing the default affects new {agent.label} sessions on {host.name}. Running
              sessions keep their connection.
            </p>
            <div className="divide-y">
              {compatible.map((provider) => {
                const isDefault =
                  !!primarySurface &&
                  inventory.effective_defaults[primarySurface] === provider.name;
                return (
                  <div
                    key={provider.name}
                    className="px-4 py-3"
                    data-testid={`agent-provider-row-${provider.name}`}
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div>
                        <p className="text-sm font-medium">
                          {provider.name}{" "}
                          {isDefault && <StatusBadge good>Used for new sessions</StatusBadge>}
                        </p>
                        <p className="text-xs text-muted-foreground">
                          {setupLabel(provider.kind)} · Saved locally
                        </p>
                      </div>
                      <div className="flex gap-2">
                        {canMutate && primarySurface && !isDefault && (
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            disabled={busy}
                            onClick={() =>
                              void onAction({
                                action: "set_default",
                                name: provider.name,
                                surface: primarySurface,
                              })
                            }
                          >
                            Use for new {agent.label} sessions
                          </Button>
                        )}
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
                          onClick={() =>
                            setManagedProvider(
                              managedProvider === provider.name ? null : provider.name,
                            )
                          }
                        >
                          Manage
                        </Button>
                      </div>
                    </div>
                    {managedProvider === provider.name && (
                      <div className="mt-3 rounded-lg border">
                        <ProviderRow
                          provider={provider}
                          effectiveDefaults={inventory.effective_defaults}
                          canMutate={canMutate}
                          busy={busy}
                          onDefault={(next) =>
                            void onAction({
                              action: "set_default",
                              name: provider.name,
                              surface: next,
                            })
                          }
                          onRemove={() => setManagedProvider(`remove:${provider.name}`)}
                        />
                      </div>
                    )}
                    {managedProvider === `remove:${provider.name}` && (
                      <ConfirmRemoval
                        provider={provider}
                        busy={busy}
                        onCancel={() => setManagedProvider(provider.name)}
                        onConfirm={() =>
                          void onAction({ action: "remove_provider", name: provider.name }).then(
                            (done) => {
                              if (done) setManagedProvider(null);
                            },
                          )
                        }
                      />
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}
        {canMutate && (
          <div className="p-4">
            <h3 className="text-sm font-medium">
              {compatible.length ? "Add connection" : "Connect"}
            </h3>
            <div className="mt-3 flex flex-wrap gap-2">
              {agent.login && (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={busy || !availableLogin || needsInstall || needsUpdate}
                  onClick={() => void onStart(agent.login!)}
                >
                  <TerminalIcon className="size-4" />{" "}
                  {agent.id === "codex"
                    ? "ChatGPT subscription"
                    : agent.id === "claude"
                      ? "Claude subscription"
                      : agent.id === "cursor" ||
                          agent.id === "antigravity" ||
                          agent.id === "kiro" ||
                          agent.id === "kimi"
                        ? `Sign in to ${agent.label}`
                        : `Configure ${agent.label}`}
                </Button>
              )}
              {agent.login && !availableLogin && !needsInstall && (
                <p className="w-full text-xs text-muted-foreground">
                  The {agent.label} setup command is unavailable on {host.name}. Check the installed
                  CLI and reconnect the host.
                </p>
              )}
              {agent.id === "pi" && (
                <div className="flex flex-col gap-1">
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={busy || needsInstall}
                    onClick={() => void onAction({ action: "subscription", cli: "pi" })}
                  >
                    Use Pi’s local configuration
                  </Button>
                  <p className="text-xs text-muted-foreground">
                    Saves local routing to Pi; it does not check your Pi sign-in.
                  </p>
                </div>
              )}
              {(agent.id === "claude" || agent.id === "codex" || agent.id === "pi") && (
                <Button
                  type="button"
                  size="sm"
                  variant={method === "key" ? "secondary" : "outline"}
                  disabled={busy}
                  onClick={() => setMethod(method === "key" ? null : "key")}
                >
                  <KeyRoundIcon className="size-4" /> API key
                </Button>
              )}
              {(agent.id === "claude" || agent.id === "codex" || agent.id === "pi") && (
                <Button
                  type="button"
                  size="sm"
                  variant={method === "gateway" ? "secondary" : "outline"}
                  disabled={busy}
                  onClick={() => setMethod(method === "gateway" ? null : "gateway")}
                >
                  Compatible gateway
                </Button>
              )}
              {agent.id === "claude" && (
                <Button
                  type="button"
                  size="sm"
                  variant={method === "bedrock" ? "secondary" : "outline"}
                  disabled={busy}
                  onClick={() => setMethod(method === "bedrock" ? null : "bedrock")}
                >
                  Bedrock
                </Button>
              )}
              {agent.id === "cursor" && (
                <Button
                  type="button"
                  size="sm"
                  variant={method === "cursor-key" ? "secondary" : "outline"}
                  disabled={busy}
                  onClick={() => setMethod(method === "cursor-key" ? null : "cursor-key")}
                >
                  Cursor API key
                </Button>
              )}
              {(["antigravity", "copilot", "opencode"] as const).includes(
                agent.id as "antigravity" | "copilot" | "opencode",
              ) && (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={busy}
                  onClick={() => {
                    const next =
                      agent.id === "antigravity"
                        ? "antigravity-key"
                        : agent.id === "copilot"
                          ? "copilot-settings"
                          : "opencode-model";
                    setMethod(method === next ? null : next);
                  }}
                >
                  {agent.id === "opencode"
                    ? "Default model"
                    : agent.id === "copilot"
                      ? "Copilot token and host"
                      : "Antigravity API key"}
                </Button>
              )}
              {(agent.id === "claude" || agent.id === "codex" || agent.id === "pi") && (
                <Button
                  type="button"
                  size="sm"
                  variant={method === "databricks" ? "secondary" : "outline"}
                  disabled={busy}
                  onClick={() => setMethod(method === "databricks" ? null : "databricks")}
                >
                  Databricks
                </Button>
              )}
            </div>
            {method && (
              <div className="mt-4 rounded-lg border bg-muted/20 p-4">
                {method === "key" && (
                  <KeyProviderForm
                    key={agent.id}
                    catalog={inventory.key_providers.filter((item) =>
                      agent.surfaces.includes(item.family as ProviderSurface),
                    )}
                    defaultModels={detection?.default_models ?? {}}
                    busy={busy}
                    onAction={onAction}
                    onDone={() => setMethod(null)}
                  />
                )}
                {method === "gateway" && (
                  <GatewayForm
                    key={agent.id}
                    initialFamily={agent.id === "claude" ? "anthropic" : "openai"}
                    busy={busy}
                    onAction={onAction}
                    onDone={() => setMethod(null)}
                  />
                )}
                {method === "bedrock" && (
                  <BedrockForm busy={busy} onAction={onAction} onDone={() => setMethod(null)} />
                )}
                {method === "cursor-key" && (
                  <HarnessKeyRow
                    harness="cursor"
                    configured={inventory.harness_settings.cursor_key_configured}
                    canMutate={canMutate}
                    busy={busy}
                    onAction={onAction}
                  />
                )}
                {method === "antigravity-key" && (
                  <HarnessKeyRow
                    harness="antigravity"
                    configured={inventory.harness_settings.antigravity_key_configured}
                    canMutate={canMutate}
                    busy={busy}
                    onAction={onAction}
                  />
                )}
                {method === "copilot-settings" && (
                  <div className="divide-y">
                    <HarnessKeyRow
                      harness="copilot"
                      configured={inventory.harness_settings.copilot_key_configured}
                      canMutate={canMutate}
                      busy={busy}
                      onAction={onAction}
                    />
                    <CopilotHostRow
                      value={inventory.harness_settings.copilot_host ?? ""}
                      canMutate={canMutate}
                      busy={busy}
                      onAction={onAction}
                    />
                  </div>
                )}
                {method === "opencode-model" && (
                  <OpenCodeModelRow
                    value={inventory.harness_settings.opencode_model ?? ""}
                    models={detection?.models.opencode ?? []}
                    canMutate={canMutate}
                    busy={busy}
                    onAction={onAction}
                  />
                )}
                {method === "databricks" && (
                  <DatabricksGuidedRow
                    initialAgent={agent.id as "claude" | "codex" | "pi"}
                    canMutate={canMutate}
                    available={operationAvailable(inventory, "databricks-configure")}
                    busy={busy}
                    onStart={(action, parameters) => onStart(action, parameters, agent.id)}
                  />
                )}
              </div>
            )}
            {agent.surfaces.length > 0 && (
              <div className="mt-4">
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  loading={detecting}
                  disabled={busy}
                  onClick={onDetect}
                >
                  <WandSparklesIcon className="size-4" /> Find credentials on this computer
                </Button>
              </div>
            )}
            {agent.surfaces.length > 0 && detection && (
              <div className="mt-3 rounded-lg border">
                <DetectionResults
                  detection={{
                    ...detection,
                    providers: detection.providers.filter((item) =>
                      detectedProviderMatchesAgent(item, agent),
                    ),
                    warnings: detection.warnings?.filter(
                      (warning) =>
                        agent.id === "claude" || warning !== CLAUDE_KEYCHAIN_DETECTION_NOTICE,
                    ),
                  }}
                  dismissed={inventory.dismissed_detections}
                  canMutate={canMutate}
                  busy={busy}
                  onAction={onAction}
                />
              </div>
            )}
            {agent.logout &&
              agent.id !== "claude" &&
              agent.id !== "codex" &&
              operationAvailable(inventory, agent.logout) && (
                <div className="mt-4 border-t pt-3">
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    disabled={busy}
                    onClick={() => void onStart(agent.logout!)}
                  >
                    Sign out of {agent.label}
                  </Button>
                </div>
              )}
          </div>
        )}
      </section>
    </div>
  );
}

function AdvancedProviderTools({
  inventory,
  detection,
  detectionRequest,
  canMutate,
  busy,
  onAction,
  onDetectImport,
  detecting,
  onDetect,
}: {
  inventory: SetupInventory;
  detection: SetupDetection | null;
  detectionRequest: SetupDetectRequest | null;
  canMutate: boolean;
  busy: boolean;
  onAction: (action: SetupAction) => Promise<boolean>;
  onDetectImport: (request: SetupDetectRequest) => void;
  detecting: boolean;
  onDetect: () => void;
}) {
  const [task, setTask] = useState<"connections" | "add" | "custom" | "import" | null>(null);
  const tasks = [
    {
      id: "connections",
      label: "Manage all connections",
      detail: "Defaults, credentials, and removal",
    },
    { id: "add", label: "Add a provider", detail: "API keys, gateways, or Bedrock" },
    { id: "custom", label: "Custom ACP agents", detail: "Add and manage launch commands" },
    { id: "import", label: "Import ACP agents", detail: "Preview OpenClaw or acpx configuration" },
  ] as const;
  const title = tasks.find((item) => item.id === task)?.label;
  return (
    <details className="rounded-xl border border-border bg-card">
      <summary className="cursor-pointer px-4 py-3 text-sm font-medium">
        Advanced provider tools
      </summary>
      <div className="border-t p-4">
        {task === null ? (
          <div className="grid gap-2 sm:grid-cols-2">
            {tasks.map((item) => (
              <button
                key={item.id}
                type="button"
                className="flex items-center justify-between gap-3 rounded-lg border border-border px-3 py-3 text-left hover:bg-muted/40"
                onClick={() => setTask(item.id)}
              >
                <span>
                  <span className="block text-sm font-medium">{item.label}</span>
                  <span className="block text-xs text-muted-foreground">{item.detail}</span>
                </span>
                <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground" />
              </button>
            ))}
          </div>
        ) : (
          <div className="flex flex-col gap-4">
            <div className="flex items-center gap-2">
              <Button type="button" size="sm" variant="ghost" onClick={() => setTask(null)}>
                <ArrowLeftIcon className="size-4" /> Back to tools
              </Button>
              <h2 className="text-sm font-medium">{title}</h2>
            </div>
            {task === "connections" && (
              <ProviderOverview
                inventory={inventory}
                canMutate={canMutate}
                busy={busy}
                detection={detection}
                detecting={detecting}
                onDetect={onDetect}
                onAction={onAction}
              />
            )}
            {task === "add" && (
              <ProviderForms
                inventory={inventory}
                detection={detection}
                canMutate={canMutate}
                busy={busy}
                onAction={onAction}
              />
            )}
            {task === "custom" && (
              <div className="rounded-lg border">
                <AcpList
                  agents={inventory.acp_agents}
                  canMutate={canMutate}
                  busy={busy}
                  onAction={onAction}
                />
                <div className="border-t">
                  <AcpForm canMutate={canMutate} busy={busy} onAction={onAction} />
                </div>
              </div>
            )}
            {task === "import" && (
              <div className="rounded-lg border">
                {canMutate && (
                  <div className="p-4">
                    <Button
                      variant="outline"
                      loading={detecting}
                      disabled={busy}
                      onClick={onDetect}
                    >
                      Find existing configuration
                    </Button>
                    <p className="mt-2 text-xs text-muted-foreground">
                      Check this computer's setup configuration, including OpenClaw and acpx.
                    </p>
                  </div>
                )}
                <ImportDetectionForm
                  canMutate={canMutate}
                  detecting={detecting}
                  onDetect={onDetectImport}
                />
                <ImportPreviewList
                  imports={detection?.imports ?? []}
                  searched={detection !== null}
                  detectionRequest={detectionRequest}
                  canMutate={canMutate}
                  busy={busy}
                  onAction={onAction}
                />
                {!!detection?.warnings?.length && (
                  <details className="border-t px-4 py-3 text-xs text-muted-foreground">
                    <summary className="cursor-pointer">Detection notes</summary>
                    {detection.warnings.map((warning) => (
                      <p key={warning} className="mt-2">
                        {warning}
                      </p>
                    ))}
                  </details>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </details>
  );
}

function HarnessKeyRow({
  harness,
  configured,
  canMutate,
  busy,
  onAction,
}: {
  harness: "cursor" | "antigravity" | "copilot";
  configured: boolean;
  canMutate: boolean;
  busy: boolean;
  onAction: (action: SetupAction) => Promise<boolean>;
}) {
  const [open, setOpen] = useState(false);
  const [secret, setSecret] = useState("");
  const [envVar, setEnvVar] = useState("");
  return (
    <div className="p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-sm font-medium capitalize">
            {harness} key {configured && <StatusBadge good>Configured locally</StatusBadge>}
          </div>
          <div className="text-xs text-muted-foreground">
            A local configuration marker only; Omnigent does not claim the vendor account is
            authenticated.
          </div>
        </div>
        {canMutate && (
          <div className="flex gap-2">
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() => setOpen(!open)}
            >
              {configured ? "Replace key" : "Add key"}
            </Button>
            {configured && (
              <Button
                type="button"
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() => void onAction({ action: "remove_harness_key", harness })}
              >
                Remove
              </Button>
            )}
          </div>
        )}
      </div>
      {open && canMutate && (
        <form
          className="mt-3 flex flex-col gap-3 rounded-lg border bg-muted/20 p-3"
          onSubmit={async (event) => {
            event.preventDefault();
            if (!(secret.trim() || envVar.trim())) return;
            if (
              await onAction({
                action: "set_harness_key",
                harness,
                ...credentialPayload(secret, envVar),
              })
            ) {
              setSecret("");
              setEnvVar("");
              setOpen(false);
            }
          }}
        >
          <SecretOrEnvironment
            secret={secret}
            envVar={envVar}
            onSecret={setSecret}
            onEnvVar={setEnvVar}
          />
          <div>
            <Button
              type="submit"
              size="sm"
              loading={busy}
              disabled={!(secret.trim() || envVar.trim())}
            >
              Save key
            </Button>
          </div>
        </form>
      )}
    </div>
  );
}

function CopilotHostRow({
  value,
  canMutate,
  busy,
  onAction,
}: {
  value: string;
  canMutate: boolean;
  busy: boolean;
  onAction: (action: SetupAction) => Promise<boolean>;
}) {
  const [host, setHost] = useState(value);
  return (
    <form
      className="flex flex-wrap items-end justify-between gap-3 p-4"
      onSubmit={(event) => {
        event.preventDefault();
        void onAction({ action: "set_copilot_host", host: host.trim() || null });
      }}
    >
      <label className="flex min-w-64 flex-1 flex-col gap-1 text-sm font-medium">
        Copilot Enterprise host
        <span className="text-xs font-normal text-muted-foreground">
          Leave blank for github.com.
        </span>
        <Input
          disabled={!canMutate || busy}
          value={host}
          onChange={(e) => setHost(e.target.value)}
          placeholder="github.example.com"
        />
      </label>
      {canMutate && (
        <Button type="submit" size="sm" variant="outline" loading={busy}>
          Save host
        </Button>
      )}
    </form>
  );
}

function OpenCodeModelRow({
  value,
  models,
  canMutate,
  busy,
  onAction,
}: {
  value: string;
  models: string[];
  canMutate: boolean;
  busy: boolean;
  onAction: (action: SetupAction) => Promise<boolean>;
}) {
  const [model, setModel] = useState(value);
  return (
    <form
      className="flex flex-wrap items-end justify-between gap-3 p-4"
      onSubmit={(event) => {
        event.preventDefault();
        void onAction({ action: "set_opencode_model", model: model.trim() || null });
      }}
    >
      <label className="flex min-w-64 flex-1 flex-col gap-1 text-sm font-medium">
        OpenCode default model
        <span className="text-xs font-normal text-muted-foreground">
          Applies to new OpenCode processes. Clear it to let OpenCode choose.
        </span>
        <Input
          disabled={!canMutate || busy}
          list="opencode-models"
          value={model}
          onChange={(e) => setModel(e.target.value)}
          placeholder="provider/model"
        />
        <datalist id="opencode-models">
          {models.map((item) => (
            <option key={item} value={item} />
          ))}
        </datalist>
      </label>
      {canMutate && (
        <Button type="submit" size="sm" variant="outline" loading={busy}>
          Save model
        </Button>
      )}
    </form>
  );
}

function DatabricksGuidedRow({
  initialAgent,
  canMutate,
  available,
  busy,
  onStart,
}: {
  initialAgent: "claude" | "codex" | "pi";
  canMutate: boolean;
  available: boolean;
  busy: boolean;
  onStart: (action: SetupOperationAction, parameters?: Record<string, unknown>) => Promise<boolean>;
}) {
  const [workspace, setWorkspace] = useState("");
  const [scope, setScope] = useState("current");
  return (
    <form
      className="flex flex-col gap-3 p-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (!available || !workspace.trim()) return;
        const agents = scope === "all" ? ["claude", "codex", "pi"] : [initialAgent];
        void onStart("databricks-configure", { workspace_url: workspace.trim(), agents });
      }}
    >
      <div>
        <div className="text-sm font-medium">Databricks workspace provider</div>
        <div className="text-xs text-muted-foreground">
          The vendor flow runs in the terminal; saved state is refreshed after it exits.
        </div>
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex min-w-64 flex-1 flex-col gap-1 text-sm font-medium">
          Workspace URL
          <Input
            disabled={!canMutate || !available || busy}
            inputMode="url"
            value={workspace}
            onChange={(e) => setWorkspace(e.target.value)}
            placeholder="https://workspace.cloud.databricks.com"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm font-medium">
          Configure for
          <Select
            value={scope}
            onValueChange={setScope}
            disabled={!canMutate || !available || busy}
          >
            <SelectTrigger className="w-56">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="current">
                {initialAgent === "claude"
                  ? "Claude Code"
                  : initialAgent === "codex"
                    ? "Codex"
                    : "Pi"}
              </SelectItem>
              <SelectItem value="all">Claude Code, Codex, and Pi</SelectItem>
            </SelectContent>
          </Select>
        </label>
        {canMutate && available && (
          <Button type="submit" size="sm" loading={busy} disabled={!workspace.trim()}>
            <TerminalIcon className="size-4" /> Configure
          </Button>
        )}
        {canMutate && !available && (
          <span className="pb-2 text-xs text-muted-foreground">
            Unavailable until the Databricks setup prerequisites are installed on this computer.
          </span>
        )}
      </div>
    </form>
  );
}

function ImportDetectionForm({
  canMutate,
  detecting,
  onDetect,
}: {
  canMutate: boolean;
  detecting: boolean;
  onDetect: (request: SetupDetectRequest) => void;
}) {
  const [source, setSource] = useState<"openclaw" | "acpx">("openclaw");
  const [path, setPath] = useState("");
  if (!canMutate) return null;
  return (
    <form
      className="flex flex-wrap items-end gap-3 border-t p-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (!path.trim()) return;
        onDetect({ import_source: source, import_path: path.trim() });
      }}
    >
      <label className="flex flex-col gap-1 text-sm font-medium">
        Import format
        <Select value={source} onValueChange={(value) => setSource(value as "openclaw" | "acpx")}>
          <SelectTrigger className="w-36" aria-label="Import format">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="openclaw">OpenClaw</SelectItem>
            <SelectItem value="acpx">acpx</SelectItem>
          </SelectContent>
        </Select>
      </label>
      <label className="flex min-w-64 flex-1 flex-col gap-1 text-sm font-medium">
        Configuration path
        <Input
          value={path}
          onChange={(event) => setPath(event.target.value)}
          placeholder="Path on the selected computer"
        />
      </label>
      <Button type="submit" variant="outline" loading={detecting} disabled={!path.trim()}>
        Preview import
      </Button>
    </form>
  );
}

function AcpForm({
  canMutate,
  busy,
  onAction,
}: {
  canMutate: boolean;
  busy: boolean;
  onAction: (action: SetupAction) => Promise<boolean>;
}) {
  const [name, setName] = useState("");
  const [command, setCommand] = useState("");
  const [model, setModel] = useState("");
  if (!canMutate) return null;
  return (
    <form
      className="flex flex-col gap-3 p-4"
      onSubmit={async (event) => {
        event.preventDefault();
        if (!name.trim() || !command.trim()) return;
        if (
          await onAction({
            action: "add_acp",
            name: name.trim(),
            command: command.trim(),
            model: model.trim() || undefined,
          })
        ) {
          setName("");
          setCommand("");
          setModel("");
        }
      }}
    >
      <h3 className="text-sm font-medium">Add custom ACP agent</h3>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="flex flex-col gap-1 text-sm">
          Name
          <Input value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Model
          <Input value={model} onChange={(e) => setModel(e.target.value)} placeholder="Optional" />
        </label>
      </div>
      <label className="flex flex-col gap-1 text-sm">
        Launch command
        <Textarea
          value={command}
          onChange={(e) => setCommand(e.target.value)}
          placeholder="my-agent --acp"
          rows={2}
        />
      </label>
      <div>
        <Button type="submit" loading={busy} disabled={!name.trim() || !command.trim()}>
          <PlusIcon className="size-4" /> Add ACP agent
        </Button>
      </div>
    </form>
  );
}

function AcpList({
  agents,
  canMutate,
  busy,
  onAction,
}: {
  agents: SetupAcpAgent[];
  canMutate: boolean;
  busy: boolean;
  onAction: (action: SetupAction) => Promise<boolean>;
}) {
  if (agents.length === 0)
    return <p className="border-t p-4 text-sm text-muted-foreground">No custom ACP agents.</p>;
  return (
    <div className="divide-y border-t">
      {agents.map((agent) => (
        <div key={agent.slug} className="flex flex-wrap items-center justify-between gap-3 p-4">
          <div>
            <div className="text-sm font-medium">{agent.name}</div>
            <code className="text-xs text-muted-foreground">{agent.command}</code>
            {agent.model && (
              <div className="text-xs text-muted-foreground">Model: {agent.model}</div>
            )}
          </div>
          {canMutate && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={busy}
              onClick={() => void onAction({ action: "remove_acp", slug: agent.slug })}
            >
              <Trash2Icon className="size-4" /> Remove
            </Button>
          )}
        </div>
      ))}
    </div>
  );
}

function ImportPreviewList({
  imports,
  searched,
  detectionRequest,
  canMutate,
  busy,
  onAction,
}: {
  imports: SetupImportPreview[];
  searched: boolean;
  detectionRequest: SetupDetectRequest | null;
  canMutate: boolean;
  busy: boolean;
  onAction: (action: SetupAction) => Promise<boolean>;
}) {
  const [selected, setSelected] = useState<string[]>([]);
  const groups = useMemo(
    () => ({
      openclaw: imports.filter((item) => item.source === "openclaw"),
      acpx: imports.filter((item) => item.source === "acpx"),
    }),
    [imports],
  );
  if (imports.length === 0)
    return (
      <p className="border-t p-4 text-sm text-muted-foreground">
        {searched
          ? "No importable agents found. You can try a configuration path."
          : "Find existing configuration or choose a path to preview agents. Credentials are never imported."}
      </p>
    );
  return (
    <div className="border-t p-4">
      <h3 className="text-sm font-medium">Import preview</h3>
      <p className="text-xs text-muted-foreground">
        Review launch commands before importing. No credentials are copied.
      </p>
      {(["openclaw", "acpx"] as const).map(
        (source) =>
          groups[source].length > 0 && (
            <div key={source} className="mt-3 rounded-lg border">
              <div className="border-b px-3 py-2 text-sm font-medium capitalize">{source}</div>
              {groups[source].map((item) => (
                <label
                  key={`${source}:${item.name}:${item.fingerprint}`}
                  className="flex items-start gap-2 border-b p-3 last:border-b-0"
                >
                  <input
                    type="checkbox"
                    disabled={!canMutate}
                    checked={selected.includes(`${source}:${item.name}:${item.fingerprint}`)}
                    onChange={(e) =>
                      setSelected((old) =>
                        e.target.checked
                          ? [...old, `${source}:${item.name}:${item.fingerprint}`]
                          : old.filter(
                              (entry) => entry !== `${source}:${item.name}:${item.fingerprint}`,
                            ),
                      )
                    }
                  />
                  <span className="min-w-0">
                    <span className="block text-sm font-medium">{item.name}</span>
                    <code className="block truncate text-xs text-muted-foreground">
                      {item.command}
                    </code>
                  </span>
                </label>
              ))}
              {canMutate && (
                <div className="border-t p-3">
                  <Button
                    type="button"
                    size="sm"
                    disabled={
                      busy ||
                      !groups[source].some((item) =>
                        selected.includes(`${source}:${item.name}:${item.fingerprint}`),
                      )
                    }
                    onClick={() =>
                      void (() => {
                        const chosen = groups[source].filter((item) =>
                          selected.includes(`${source}:${item.name}:${item.fingerprint}`),
                        );
                        return onAction({
                          action: "import_acp",
                          source,
                          names: chosen.map((item) => item.name),
                          path:
                            detectionRequest?.import_source === source
                              ? detectionRequest.import_path
                              : undefined,
                          fingerprints: Object.fromEntries(
                            chosen.map((item) => [item.name, item.fingerprint]),
                          ),
                        });
                      })()
                    }
                  >
                    Import selected {source} agents
                  </Button>
                </div>
              )}
            </div>
          ),
      )}
    </div>
  );
}
