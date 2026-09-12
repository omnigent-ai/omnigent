// Model providers & per-agent pins management — the settings surface for
// the host config control plane (issue #7134). Talks to
// `/v1/hosts/{id}/providers*` and `/v1/hosts/{id}/agent-specs*` via
// `lib/hostProvidersApi.ts`.
//
// The host is the source of truth: validation, credential resolution, and
// endpoint probing all run host-side. This surface edits forms, shows
// probe results, and pins agents — it never sees a secret value (the API
// returns `api_key_set` descriptors instead).

import { useEffect, useMemo, useState } from "react";
import { PencilIcon, PlusIcon, Trash2Icon, TriangleAlertIcon, ZapIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/scheduled/Label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useHosts, type Host } from "@/hooks/useHosts";
import {
  useClearHostAgentPin,
  useDeleteHostProvider,
  useHostAgentSpecs,
  useHostProviders,
  usePinHostAgent,
  useTestHostProvider,
  useUpsertHostProvider,
} from "@/hooks/useHostProviders";
import type { HostAgentSpec, HostProvider, ProviderTestResult } from "@/lib/hostProvidersApi";

const FAMILY_KINDS = new Set(["key", "gateway", "local"]);
const FAMILIES = ["openai", "anthropic", "gemini"] as const;
const PROVIDER_KINDS = ["key", "gateway", "local", "subscription"] as const;
const SECRET_MODES = ["env", "keychain", "inline"] as const;
type SecretMode = (typeof SECRET_MODES)[number];

/** One family block of an entry, as the redacted wire returns it. */
function familyBlock(entry: HostProvider): Record<string, unknown> | null {
  for (const family of FAMILIES) {
    const block = entry[family];
    if (block && typeof block === "object") return block as Record<string, unknown>;
  }
  return null;
}

function baseUrlOf(entry: HostProvider): string {
  const block = familyBlock(entry);
  const url = block?.base_url;
  return typeof url === "string" ? url : "";
}

function defaultModelOf(entry: HostProvider): string {
  const block = familyBlock(entry);
  const models = block?.models;
  if (models && typeof models === "object" && !Array.isArray(models)) {
    const def = (models as Record<string, unknown>).default;
    if (typeof def === "string") return def;
  }
  return "";
}

function modelsOf(entry: HostProvider): string[] {
  const block = familyBlock(entry);
  const models = block?.models;
  if (models && typeof models === "object" && !Array.isArray(models)) {
    return Object.entries(models as Record<string, unknown>)
      .filter(([key, value]) => key !== "default" && typeof value === "string")
      .map(([, value]) => String(value));
  }
  return [];
}

function familyOf(entry: HostProvider): string {
  for (const family of FAMILIES) {
    if (entry[family] && typeof entry[family] === "object") return family;
  }
  return "openai";
}

/** Parse a redacted entry back into form state for editing. */
function formStateFrom(entry: HostProvider): {
  kind: string;
  family: string;
  baseUrl: string;
  secretMode: SecretMode;
  secretValue: string;
  wireApi: string;
  defaultModel: string;
  isDefault: boolean;
} {
  const block = familyBlock(entry) ?? {};
  let secretMode: SecretMode = "env";
  let secretValue = "";
  if (typeof block.api_key_ref === "string") {
    secretMode = block.api_key_ref.startsWith("keychain:") ? "keychain" : "env";
    secretValue = block.api_key_ref;
  } else if (block.api_key_set === true) {
    secretMode = "inline";
    secretValue = ""; // the value never came back; leave blank unless replaced
  } else if (typeof block.auth_command === "string") {
    secretMode = "env"; // not editable here; treated as-is
    secretValue = String(block.auth_command);
  }
  const wireApi = typeof block.wire_api === "string" ? block.wire_api : "chat";
  const def = entry.default;
  return {
    kind: typeof entry.kind === "string" ? entry.kind : "gateway",
    family: familyOf(entry),
    baseUrl: baseUrlOf(entry),
    secretMode,
    secretValue,
    wireApi,
    defaultModel: defaultModelOf(entry),
    isDefault: def === true || (Array.isArray(def) && def.length > 0) || typeof def === "string",
  };
}

function buildEntry(form: {
  kind: string;
  family: string;
  baseUrl: string;
  secretMode: SecretMode;
  secretValue: string;
  wireApi: string;
  defaultModel: string;
  isDefault: boolean;
}): Record<string, unknown> {
  const entry: Record<string, unknown> = { kind: form.kind };
  if (form.isDefault) entry.default = true;
  if (form.kind === "subscription") return entry;
  const block: Record<string, unknown> = {};
  if (form.baseUrl.trim()) block.base_url = form.baseUrl.trim();
  if (form.secretMode === "env" && form.secretValue.trim()) {
    block.api_key_ref = form.secretValue.trim().startsWith("env:")
      ? form.secretValue.trim()
      : `env:${form.secretValue.trim()}`;
  } else if (form.secretMode === "keychain" && form.secretValue.trim()) {
    block.api_key_ref = form.secretValue.trim().startsWith("keychain:")
      ? form.secretValue.trim()
      : `keychain:${form.secretValue.trim()}`;
  } else if (form.secretMode === "inline" && form.secretValue.trim()) {
    block.api_key = form.secretValue.trim();
  }
  if (form.family === "openai") block.wire_api = form.wireApi;
  const models: Record<string, string> = {};
  if (form.defaultModel.trim()) models.default = form.defaultModel.trim();
  if (Object.keys(models).length > 0) block.models = models;
  entry[form.family] = block;
  return entry;
}

interface ProviderFormDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  hostId: string;
  editing: HostProvider | null;
}

function ProviderFormDialog({ open, onOpenChange, hostId, editing }: ProviderFormDialogProps) {
  const [name, setName] = useState("");
  const [kind, setKind] = useState<string>("gateway");
  const [family, setFamily] = useState<string>("openai");
  const [baseUrl, setBaseUrl] = useState("");
  const [secretMode, setSecretMode] = useState<SecretMode>("env");
  const [secretValue, setSecretValue] = useState("");
  const [wireApi, setWireApi] = useState("chat");
  const [defaultModel, setDefaultModel] = useState("");
  const [isDefault, setIsDefault] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const upsert = useUpsertHostProvider(hostId);

  useEffect(() => {
    if (!open) return;
    setError(null);
    if (editing) {
      setName(editing.name);
      const state = formStateFrom(editing);
      setKind(state.kind);
      setFamily(state.family);
      setBaseUrl(state.baseUrl);
      setSecretMode(state.secretMode);
      setSecretValue(state.secretValue);
      setWireApi(state.wireApi);
      setDefaultModel(state.defaultModel);
      setIsDefault(state.isDefault);
    } else {
      setName("");
      setKind("gateway");
      setFamily("openai");
      setBaseUrl("");
      setSecretMode("env");
      setSecretValue("");
      setWireApi("chat");
      setDefaultModel("");
      setIsDefault(false);
    }
  }, [open, editing]);

  const submit = () => {
    setError(null);
    if (!name.trim()) {
      setError("Name is required.");
      return;
    }
    const entry = buildEntry({
      kind,
      family,
      baseUrl,
      secretMode,
      secretValue,
      wireApi,
      defaultModel,
      isDefault,
    });
    upsert.mutate(
      { name: name.trim(), entry },
      {
        onSuccess: () => onOpenChange(false),
        onError: (err) => setError(err.message),
      },
    );
  };

  const familyFields = FAMILY_KINDS.has(kind);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{editing ? `Edit provider ${editing.name}` : "Add provider"}</DialogTitle>
          <DialogDescription>
            Written to the host&apos;s <code>~/.omnigent/config.yaml</code> and validated with the
            same parser the runtime uses. API keys are stored host-side.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-4">
          <div className="grid gap-2">
            <Label htmlFor="provider-name">Name</Label>
            <Input
              id="provider-name"
              value={name}
              disabled={editing !== null}
              onChange={(e) => setName(e.target.value)}
              placeholder="openrouter"
            />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div className="grid gap-2">
              <Label>Kind</Label>
              <Select value={kind} onValueChange={setKind}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {PROVIDER_KINDS.map((k) => (
                    <SelectItem key={k} value={k}>
                      {k}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {familyFields && (
              <div className="grid gap-2">
                <Label>Family</Label>
                <Select value={family} onValueChange={setFamily}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {FAMILIES.map((f) => (
                      <SelectItem key={f} value={f}>
                        {f}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
          </div>
          {familyFields && (
            <>
              <div className="grid gap-2">
                <Label htmlFor="provider-url">Base URL / endpoint</Label>
                <Input
                  id="provider-url"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder="https://openrouter.ai/api/v1"
                />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="grid gap-2">
                  <Label>API key source</Label>
                  <Select value={secretMode} onValueChange={(v) => setSecretMode(v as SecretMode)}>
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="env">Environment variable</SelectItem>
                      <SelectItem value="keychain">Keychain secret</SelectItem>
                      <SelectItem value="inline">Inline key</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="provider-secret">
                    {secretMode === "env"
                      ? "Variable name"
                      : secretMode === "keychain"
                        ? "Secret name"
                        : "API key"}
                  </Label>
                  <Input
                    id="provider-secret"
                    value={secretValue}
                    onChange={(e) => setSecretValue(e.target.value)}
                    placeholder={
                      secretMode === "env"
                        ? "OPENROUTER_API_KEY"
                        : secretMode === "keychain"
                          ? "anthropic"
                          : "sk-…"
                    }
                  />
                </div>
              </div>
              {family === "openai" && (
                <div className="grid grid-cols-2 gap-4">
                  <div className="grid gap-2">
                    <Label>Wire API</Label>
                    <Select value={wireApi} onValueChange={setWireApi}>
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="chat">chat</SelectItem>
                        <SelectItem value="responses">responses</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="provider-model">Default model</Label>
                    <Input
                      id="provider-model"
                      value={defaultModel}
                      onChange={(e) => setDefaultModel(e.target.value)}
                      placeholder="gpt-4o"
                    />
                  </div>
                </div>
              )}
              <div className="flex items-center gap-2">
                <Switch id="provider-default" checked={isDefault} onCheckedChange={setIsDefault} />
                <Label htmlFor="provider-default">Default provider for this family</Label>
              </div>
            </>
          )}
          {editing && secretMode === "inline" && secretValue === "" && (
            <p className="text-muted-foreground text-xs">
              The stored key is never shown. Leave blank to keep it, or type a new key to replace
              it.
            </p>
          )}
          {error && (
            <p className="text-destructive flex items-center gap-1 text-sm">
              <TriangleAlertIcon className="size-4" /> {error}
            </p>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={upsert.isPending}>
            {editing ? "Save" : "Add"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

interface AgentPinDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  hostId: string;
  agent: HostAgentSpec | null;
  providers: HostProvider[];
}

function AgentPinDialog({ open, onOpenChange, hostId, agent, providers }: AgentPinDialogProps) {
  const [provider, setProvider] = useState<string>("");
  const [model, setModel] = useState("");
  const [error, setError] = useState<string | null>(null);
  const pin = usePinHostAgent(hostId);

  useEffect(() => {
    if (!open || !agent) return;
    setError(null);
    setProvider(agent.auth?.type === "provider" && agent.auth.name ? agent.auth.name : "");
    setModel(agent.model ?? "");
  }, [open, agent]);

  const selected = useMemo(
    () => providers.find((p) => p.name === provider) ?? null,
    [providers, provider],
  );
  const suggestions = useMemo(
    () => (selected ? [defaultModelOf(selected), ...modelsOf(selected)].filter(Boolean) : []),
    [selected],
  );

  const submit = () => {
    setError(null);
    pin.mutate(
      {
        agent: (agent as HostAgentSpec).name,
        pin: { provider: provider || null, model: model || null },
      },
      {
        onSuccess: () => onOpenChange(false),
        onError: (err) => setError(err.message),
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Pin {agent?.name}</DialogTitle>
          <DialogDescription>
            A provider/model pin overrides every ambient default for this agent — it wins over the
            host default and the provider&apos;s own default model.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-4">
          <div className="grid gap-2">
            <Label>Provider</Label>
            <Select value={provider} onValueChange={setProvider}>
              <SelectTrigger>
                <SelectValue placeholder="Host default" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="none">Host default (no pin)</SelectItem>
                {providers.map((p) => (
                  <SelectItem key={p.name} value={p.name}>
                    {p.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="pin-model">Model</Label>
            <Input
              id="pin-model"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              list="pin-model-suggestions"
              placeholder={selected ? defaultModelOf(selected) || "model id" : "model id"}
            />
            <datalist id="pin-model-suggestions">
              {suggestions.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </div>
          {error && (
            <p className="text-destructive flex items-center gap-1 text-sm">
              <TriangleAlertIcon className="size-4" /> {error}
            </p>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={pin.isPending}>
            Save pin
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Section body shown when the selected host is reachable. */
function HostConfigPanels({ hostId }: { hostId: string }) {
  const providersQuery = useHostProviders(hostId);
  const agentsQuery = useHostAgentSpecs(hostId);
  const deleteProvider = useDeleteHostProvider(hostId);
  const testProvider = useTestHostProvider(hostId);
  const clearPin = useClearHostAgentPin(hostId);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<HostProvider | null>(null);
  const [pinAgent, setPinAgent] = useState<HostAgentSpec | null>(null);
  const [testResults, setTestResults] = useState<Record<string, ProviderTestResult | "pending">>(
    {},
  );

  const providers = providersQuery.data ?? [];
  const agents = agentsQuery.data ?? [];

  const runTest = (name: string) => {
    setTestResults((prev) => ({ ...prev, [name]: "pending" }));
    testProvider.mutate(name, {
      onSuccess: (result) => setTestResults((prev) => ({ ...prev, [name]: result })),
      onError: (err) =>
        setTestResults((prev) => ({
          ...prev,
          [name]: { name, family: "", endpoint: "", ok: false, error: err.message },
        })),
    });
  };

  return (
    <div className="grid gap-8">
      <div>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-lg font-medium">Providers</h2>
          <Button
            variant="outline"
            onClick={() => {
              setEditing(null);
              setFormOpen(true);
            }}
          >
            <PlusIcon className="size-4" /> Add provider
          </Button>
        </div>
        {providersQuery.isError && (
          <p className="text-destructive text-sm">{(providersQuery.error as Error).message}</p>
        )}
        {providers.length === 0 && !providersQuery.isError && (
          <p className="text-muted-foreground text-sm">No providers configured on this host yet.</p>
        )}
        <div className="grid gap-2">
          {providers.map((entry) => {
            const result = testResults[entry.name];
            return (
              <div
                key={entry.name}
                className="flex items-center justify-between gap-4 rounded-md border px-4 py-3"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium">{entry.name}</span>
                    {typeof entry.kind === "string" && (
                      <Badge variant="secondary">{entry.kind}</Badge>
                    )}
                    {entry.default === true && <Badge>default</Badge>}
                  </div>
                  <p className="text-muted-foreground truncate text-sm">
                    {baseUrlOf(entry) || familyOf(entry)}
                  </p>
                  {result === "pending" && (
                    <p className="text-muted-foreground text-xs">Testing…</p>
                  )}
                  {result && result !== "pending" && (
                    <p
                      className={
                        result.ok
                          ? "text-xs text-green-600 dark:text-green-400"
                          : "text-destructive text-xs"
                      }
                    >
                      {result.ok
                        ? `OK ${result.http_status} · ${result.latency_ms}ms · ${result.models?.length ?? 0} models via ${result.endpoint}`
                        : `Failed: ${result.error ?? `HTTP ${result.http_status}`}`}
                    </p>
                  )}
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => runTest(entry.name)}
                    disabled={result === "pending"}
                  >
                    <ZapIcon className="size-4" /> Test
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      setEditing(entry);
                      setFormOpen(true);
                    }}
                  >
                    <PencilIcon className="size-4" /> Edit
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => deleteProvider.mutate(entry.name)}
                    disabled={deleteProvider.isPending}
                  >
                    <Trash2Icon className="size-4" /> Delete
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      </div>
      <div>
        <h2 className="mb-3 text-lg font-medium">Agent specs</h2>
        {agentsQuery.isError && (
          <p className="text-destructive text-sm">{(agentsQuery.error as Error).message}</p>
        )}
        {agents.length === 0 && !agentsQuery.isError && (
          <p className="text-muted-foreground text-sm">
            No agent specs under <code>~/.omnigent/agents/</code> on this host.
          </p>
        )}
        <div className="grid gap-2">
          {agents.map((agent) => (
            <div
              key={agent.name}
              className="flex items-center justify-between gap-4 rounded-md border px-4 py-3"
            >
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-medium">{agent.name}</span>
                  {agent.harness && <Badge variant="secondary">{agent.harness}</Badge>}
                </div>
                <p className="text-muted-foreground text-sm">
                  {agent.model ?? "no model pin"}
                  {agent.auth?.type === "provider" && agent.auth.name
                    ? ` · provider: ${agent.auth.name}`
                    : ""}
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <Button variant="ghost" size="sm" onClick={() => setPinAgent(agent)}>
                  <PencilIcon className="size-4" /> Pin
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => clearPin.mutate(agent.name)}
                  disabled={clearPin.isPending || (agent.auth?.type !== "provider" && !agent.model)}
                >
                  <Trash2Icon className="size-4" /> Clear
                </Button>
              </div>
            </div>
          ))}
        </div>
      </div>
      <ProviderFormDialog
        open={formOpen}
        onOpenChange={setFormOpen}
        hostId={hostId}
        editing={editing}
      />
      <AgentPinDialog
        open={pinAgent !== null}
        onOpenChange={(open) => {
          if (!open) setPinAgent(null);
        }}
        hostId={hostId}
        agent={pinAgent}
        providers={providers}
      />
    </div>
  );
}

/** Settings → Models: per-host providers and per-agent pins. */
export function ModelProvidersSection() {
  const { data: hosts, isLoading: hostsLoading } = useHosts();
  const online = useMemo(() => (hosts ?? []).filter((h: Host) => h.status === "online"), [hosts]);
  const [hostId, setHostId] = useState<string | null>(null);
  const effectiveHostId = hostId ?? online[0]?.host_id ?? null;

  return (
    <section>
      <h1 className="text-2xl font-semibold">Models &amp; providers</h1>
      <p className="mt-1 text-muted-foreground text-ui">
        Manage model providers on each connected host and pin a provider and model per agent.
        Changes are written to the host&apos;s own configuration and validated there.
      </p>
      <div className="mt-6 grid gap-6">
        <div className="grid gap-2 sm:max-w-xs">
          <Label>Host</Label>
          <Select value={effectiveHostId ?? undefined} onValueChange={(v) => setHostId(v)}>
            <SelectTrigger disabled={hostsLoading || online.length === 0}>
              <SelectValue placeholder={hostsLoading ? "Loading…" : "Select a host"} />
            </SelectTrigger>
            <SelectContent>
              {online.map((h) => (
                <SelectItem key={h.host_id} value={h.host_id}>
                  {h.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        {effectiveHostId ? (
          <HostConfigPanels hostId={effectiveHostId} />
        ) : (
          !hostsLoading && (
            <p className="text-muted-foreground text-sm">
              No online hosts. Connect a host daemon to manage its providers.
            </p>
          )
        )}
      </div>
    </section>
  );
}
