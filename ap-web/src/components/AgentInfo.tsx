// Agent info surface: the MCP-server and policy badges plus the
// header info-icon popover that displays them.

import { useEffect, useRef, useState } from "react";
import {
  CheckIcon,
  CopyIcon,
  PencilIcon,
  InfoIcon,
  PlusIcon,
  SaveIcon,
  ServerIcon,
  ShieldCheckIcon,
  TrashIcon,
  XIcon,
} from "lucide-react";
import {
  useCreateMcpServer,
  useDeleteMcpServer,
  useUpdateMcpServer,
  type UpsertMcpServerInput,
} from "@/hooks/useAgents";
import type { Agent, McpServerSummary } from "@/hooks/useAgents";
import type { ModelUsage } from "@/lib/types";
import {
  usePolicies,
  usePolicyRegistry,
  useAddPolicy,
  useDeletePolicy,
  type PolicyRegistryEntry,
} from "@/hooks/usePolicies";
import { useSessionOwner } from "@/hooks/usePermissions";
import { getCurrentUserId } from "@/lib/identity";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { capitalizeAgentName } from "@/lib/agentLabels";
import { coercePolicyParams } from "@/lib/policyParams";
import { agentRootName } from "@/lib/forkHarness";
import { nativeCodingAgentForAgentName } from "@/lib/nativeCodingAgents";
import { copyText } from "@/lib/clipboard";
import { useChatStore } from "@/store/chatStore";

/**
 * Display label for an agent name: the wrapper alias when mapped, else
 * the name capital-first (server agent names are lowercase slugs, e.g.
 * ``"polly"`` → ``"Polly"``). Keeps the chat surfaces consistent with
 * the new-chat picker's capitalization.
 *
 * Strips EVERY `" (fork <id>)"` / `" (switch <id>)"` suffix the fork/switch
 * routes append to a cloned agent's name before resolving (a fork of a fork
 * nests them), so a clone of a native wrapper (e.g.
 * `"pi-native-ui (fork conv_a) (fork conv_b)"`) still maps to its display
 * name ("Pi") instead of falling through to the capitalized raw slug
 * ("Pi-native-ui (fork conv_a) …"). Mirrors how `useAvailableAgents` and the
 * fork/switch pickers match clones back to their root agent.
 */
export function agentDisplayLabel(name: string): string {
  const baseName = agentRootName(name);
  const nativeAgent = nativeCodingAgentForAgentName(baseName);
  if (nativeAgent?.key === "claude") return "Claude";
  return nativeAgent?.displayName ?? capitalizeAgentName(baseName);
}

/** Compact pill row listing MCP servers attached to an agent. */
export function McpServerList({
  servers,
  onDelete,
}: {
  servers: McpServerSummary[];
  onDelete?: (name: string) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1">
      {servers.map((srv) =>
        onDelete ? (
          <Popover key={srv.name}>
            <PopoverTrigger asChild>
              <button
                type="button"
                className="flex cursor-pointer items-center gap-0.5 rounded-full border border-border bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:bg-muted/80"
                onClick={(e) => e.stopPropagation()}
              >
                <ServerIcon className="size-2.5 shrink-0" />
                {srv.name}
              </button>
            </PopoverTrigger>
            <PopoverContent
              side="top"
              align="start"
              className="w-64"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="flex flex-col gap-2">
                <div className="flex items-center gap-1.5">
                  <ServerIcon className="size-3.5 text-muted-foreground" />
                  <span className="font-medium text-sm">{srv.name}</span>
                </div>
                {srv.description && (
                  <p className="text-xs text-muted-foreground">{srv.description}</p>
                )}
                <button
                  type="button"
                  onClick={() => onDelete(srv.name)}
                  className="flex items-center gap-1 self-end rounded px-2 py-1 text-xs text-destructive hover:bg-destructive/10"
                >
                  <TrashIcon className="size-3" />
                  Remove
                </button>
              </div>
            </PopoverContent>
          </Popover>
        ) : (
          <span
            key={srv.name}
            title={srv.description ?? srv.name}
            className="flex items-center gap-0.5 rounded-full border border-border bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground"
          >
            <ServerIcon className="size-2.5 shrink-0" />
            {srv.name}
          </span>
        ),
      )}
    </div>
  );
}

/** Small uppercase section label inside the agent-info popover. */
function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground/70">
      {children}
    </span>
  );
}

/** Format cumulative session spend: `$x.xx`, or `<$0.01` for sub-cent. */
function formatSessionCostUsd(costUsd: number): string {
  if (costUsd > 0 && costUsd < 0.01) {
    // Genuinely priced but rounds to $0.00 — distinguish from free.
    return "<$0.01";
  }
  return `$${costUsd.toFixed(2)}`;
}

/**
 * Compact token-count formatter for the usage breakdown, e.g. ``842`` →
 * ``"842"``, ``12_400`` → ``"12.4K"``, ``1_530_000`` → ``"1.5M"``. Keeps
 * the popover rows narrow while staying readable. Small counts (< 1000)
 * render in full so they aren't misleadingly rounded.
 */
function formatTokenCount(tokens: number): string {
  return new Intl.NumberFormat(undefined, {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(tokens);
}

/**
 * Token buckets shown per model in the ``usage_by_model`` section, mapping
 * the ``ModelUsage`` field to its row label. Cost is rendered separately.
 */
const MODEL_TOKEN_ROWS: ReadonlyArray<{ key: keyof ModelUsage; label: string }> = [
  { key: "inputTokens", label: "Input" },
  { key: "outputTokens", label: "Output" },
  { key: "cacheReadInputTokens", label: "Cache read" },
  { key: "cacheCreationInputTokens", label: "Cache write" },
  { key: "totalTokens", label: "Total" },
];

/**
 * Per-model usage breakdown: one labeled group per model, each listing its
 * recorded token buckets (and USD cost when the model was priced). Rendered
 * beneath the aggregate token breakdown. The caller decides whether to show
 * this at all (it's redundant with the aggregate when only one model ran).
 *
 * @param usageByModel - Map of raw harness model id to its cumulative usage.
 */
function ModelUsageBreakdown({ usageByModel }: { usageByModel: Record<string, ModelUsage> }) {
  const [isOpen, setIsOpen] = useState(false);
  // Stable display order: most total tokens first, so the dominant model
  // leads. Falls back to 0 for models that haven't recorded a total yet.
  const models = Object.entries(usageByModel).sort(
    ([, a], [, b]) => (b.totalTokens ?? 0) - (a.totalTokens ?? 0),
  );
  return (
    <details
      data-testid="agent-info-usage-by-model"
      onToggle={(e) => setIsOpen(e.currentTarget.open)}
    >
      <summary className="cursor-pointer select-none list-none">
        <SectionLabel>
          <span className="inline-flex items-center gap-1">
            Token usage
            <span className="text-[9px]">{isOpen ? "▼" : "▶"}</span>
          </span>
        </SectionLabel>
      </summary>
      <div className="mt-1.5 flex flex-col gap-2">
        {models.map(([model, usage]) => {
          const rows = MODEL_TOKEN_ROWS.flatMap(({ key, label }) => {
            const value = usage[key];
            return value != null ? [{ label, value }] : [];
          });
          return (
            <div
              key={model}
              className="flex flex-col gap-0.5"
              data-testid={`agent-info-model-${model}`}
            >
              <span className="truncate font-mono text-[11px] text-muted-foreground" title={model}>
                {model}
              </span>
              {rows.map((row) => (
                <div
                  key={row.label}
                  className="flex items-baseline justify-between gap-3 pl-2 text-xs"
                >
                  <span className="text-muted-foreground/70">{row.label}</span>
                  <span className="tabular-nums text-muted-foreground">
                    {formatTokenCount(row.value)}
                  </span>
                </div>
              ))}
              {usage.totalCostUsd != null && (
                <div className="flex items-baseline justify-between gap-3 pl-2 text-xs">
                  <span className="text-muted-foreground/70">Cost</span>
                  <span className="tabular-nums text-muted-foreground">
                    {formatSessionCostUsd(usage.totalCostUsd)}
                  </span>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </details>
  );
}

// ---------------------------------------------------------------------------
// Add-policy dialog
// ---------------------------------------------------------------------------

function AddPolicyDialog({
  sessionId,
  registry,
  appliedHandlers,
  open,
  onOpenChange,
}: {
  sessionId: string;
  registry: PolicyRegistryEntry[];
  appliedHandlers: Set<string>;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [selected, setSelected] = useState<string>("");
  const [filter, setFilter] = useState("");
  const [factoryParams, setFactoryParams] = useState<Record<string, string>>({});
  const [paramError, setParamError] = useState<string | null>(null);
  const addPolicy = useAddPolicy(sessionId);

  const entry = registry.find((r) => r.handler === selected);
  const schema = entry?.params_schema as
    | {
        properties?: Record<
          string,
          {
            type?: string;
            description?: string;
            default?: unknown;
            enum?: string[];
            items?: { type?: string; enum?: string[] };
            uniqueItems?: boolean;
          }
        >;
        required?: string[];
      }
    | null
    | undefined;
  const properties = schema?.properties ?? {};
  const paramKeys = Object.keys(properties);

  function handleSelect(handler: string) {
    setSelected(handler);
    setFilter("");
    setFactoryParams({});
    setParamError(null);
  }

  function handleAdd() {
    if (!entry) return;
    let parsedParams: Record<string, unknown> | undefined;
    if (entry.kind === "factory" && paramKeys.length > 0) {
      const result = coercePolicyParams(paramKeys, properties, factoryParams);
      if (!result.ok) {
        setParamError(result.error);
        return;
      }
      parsedParams = result.params;
    }
    setParamError(null);
    // Always send factory_params for factory-kind policies (even
    // if empty) so the stored entity has ``factory_params={}``
    // instead of ``None``. The builder uses ``arguments is not
    // None`` to distinguish factory form (invoke with kwargs)
    // from direct-callable form (use as-is). Without this,
    // factories like ``deny_pii_in_llm_request`` are called as
    // ``factory(event)`` instead of ``factory()(event)``.
    const includeFactoryParams =
      entry.kind === "factory" ? { factory_params: parsedParams ?? {} } : {};
    addPolicy.mutate(
      {
        name: entry.name.toLowerCase().replace(/\s+/g, "_"),
        type: "python",
        handler: entry.handler,
        ...includeFactoryParams,
      },
      {
        onSuccess: () => {
          setSelected("");
          setFactoryParams({});
          onOpenChange(false);
        },
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[80vh] overflow-y-auto sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Add Policy</DialogTitle>
          <DialogDescription>Choose a policy to apply to this session.</DialogDescription>
        </DialogHeader>
        <div className="space-y-3 pt-1">
          {!selected &&
            (() => {
              const available = registry.filter((r) => !appliedHandlers.has(r.handler));
              const lowerFilter = filter.toLowerCase();
              const filtered = lowerFilter
                ? available.filter(
                    (r) =>
                      r.name.toLowerCase().includes(lowerFilter) ||
                      r.description?.toLowerCase().includes(lowerFilter),
                  )
                : available;
              return (
                <>
                  <input
                    type="text"
                    value={filter}
                    onChange={(e) => setFilter(e.target.value)}
                    placeholder="Filter policies..."
                    className="w-full rounded border border-border bg-background px-2 py-1.5 text-sm placeholder:text-muted-foreground/60 focus:outline-none focus:ring-1 focus:ring-ring"
                    // eslint-disable-next-line jsx-a11y/no-autofocus
                    autoFocus
                  />
                  <div className="flex max-h-52 flex-col divide-y divide-border overflow-y-auto rounded border border-border">
                    {filtered.map((r) => (
                      <button
                        key={r.handler}
                        type="button"
                        onClick={() => handleSelect(r.handler)}
                        className="flex flex-col gap-0.5 px-2.5 py-2 text-left hover:bg-muted"
                      >
                        <span className="text-sm">{r.name}</span>
                        {r.description && (
                          <span className="line-clamp-2 text-[11px] text-muted-foreground">
                            {r.description}
                          </span>
                        )}
                      </button>
                    ))}
                    {filtered.length === 0 && (
                      <p className="py-2 text-center text-xs text-muted-foreground">
                        {available.length === 0
                          ? "All available policies are already applied."
                          : "No policies match your filter."}
                      </p>
                    )}
                  </div>
                </>
              );
            })()}
          {entry && (
            <div className="flex flex-col gap-1 rounded border border-border bg-muted/50 px-2.5 py-2">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium">{entry.name}</span>
                <button
                  type="button"
                  onClick={() => {
                    setSelected("");
                    setFactoryParams({});
                    setParamError(null);
                  }}
                  className="text-[11px] text-muted-foreground hover:text-foreground"
                >
                  Change
                </button>
              </div>
              {entry.description && (
                <p className="text-xs text-muted-foreground">{entry.description}</p>
              )}
            </div>
          )}
          {entry?.kind === "factory" && paramKeys.length > 0 && (
            <div className="space-y-2">
              {paramKeys.map((key) => {
                const prop = properties[key];
                return (
                  <div key={key}>
                    <label className="flex items-center gap-1 text-xs text-muted-foreground">
                      <span className="font-medium text-foreground">{key}</span>
                      {prop?.type && (
                        <span>
                          (
                          {prop.type === "array" && prop.items?.enum
                            ? "select"
                            : prop.type === "array"
                              ? "comma-separated"
                              : prop.type}
                          )
                        </span>
                      )}
                    </label>
                    {prop?.description && (
                      <p className="text-[11px] text-muted-foreground">{prop.description}</p>
                    )}
                    {prop?.type === "boolean" ? (
                      <select
                        value={
                          factoryParams[key] ??
                          (prop?.default !== undefined ? String(prop.default) : "")
                        }
                        onChange={(e) =>
                          setFactoryParams((prev) => ({
                            ...prev,
                            [key]: e.target.value,
                          }))
                        }
                        className="mt-0.5 w-full rounded border border-border bg-background px-2 py-1.5 text-sm"
                      >
                        <option value="true">true</option>
                        <option value="false">false</option>
                      </select>
                    ) : prop?.type === "string" && prop.enum ? (
                      <select
                        value={
                          factoryParams[key] ??
                          (prop?.default !== undefined
                            ? String(prop.default)
                            : (prop.enum[0] ?? ""))
                        }
                        onChange={(e) =>
                          setFactoryParams((prev) => ({
                            ...prev,
                            [key]: e.target.value,
                          }))
                        }
                        className="mt-0.5 w-full rounded border border-border bg-background px-2 py-1.5 text-sm"
                      >
                        {prop.enum.map((v) => (
                          <option key={v} value={v}>
                            {v}
                          </option>
                        ))}
                      </select>
                    ) : prop?.type === "array" && prop.items?.enum ? (
                      <div className="mt-0.5 flex flex-wrap gap-x-3 gap-y-1">
                        {prop.items.enum.map((v) => {
                          const current = factoryParams[key]
                            ? factoryParams[key].split(",").filter(Boolean)
                            : Array.isArray(prop?.default)
                              ? (prop.default as string[])
                              : [];
                          const checked = current.includes(v);
                          return (
                            <label key={v} className="flex items-center gap-1 text-sm">
                              <input
                                type="checkbox"
                                checked={checked}
                                onChange={(e) => {
                                  const next = e.target.checked
                                    ? [...current, v]
                                    : current.filter((x) => x !== v);
                                  setFactoryParams((prev) => ({
                                    ...prev,
                                    [key]: next.join(","),
                                  }));
                                }}
                                className="rounded border-border"
                              />
                              <span>{v}</span>
                            </label>
                          );
                        })}
                      </div>
                    ) : (
                      <input
                        type={
                          prop?.type === "integer" || prop?.type === "number" ? "number" : "text"
                        }
                        placeholder={
                          prop?.type === "array"
                            ? prop?.default !== undefined
                              ? (prop.default as string[]).join(", ")
                              : "comma-separated values"
                            : prop?.default !== undefined
                              ? String(prop.default)
                              : ""
                        }
                        value={factoryParams[key] ?? ""}
                        onChange={(e) =>
                          setFactoryParams((prev) => ({
                            ...prev,
                            [key]: e.target.value,
                          }))
                        }
                        className="mt-0.5 w-full rounded border border-border bg-background px-2 py-1.5 text-sm"
                      />
                    )}
                  </div>
                );
              })}
            </div>
          )}
          {(paramError || addPolicy.isError) && (
            <div
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
            >
              {paramError ?? addPolicy.error?.message}
            </div>
          )}
          <div className="flex justify-end gap-2 pt-1">
            <button
              type="button"
              onClick={() => onOpenChange(false)}
              className="rounded px-3 py-1.5 text-xs hover:bg-muted"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleAdd}
              disabled={!selected || addPolicy.isPending}
              className="rounded bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-50"
            >
              {addPolicy.isPending ? "Adding..." : "Add"}
            </button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// MCP server management
// ---------------------------------------------------------------------------

interface McpFormState {
  originalName: string | null;
  name: string;
  transport: "http" | "stdio";
  description: string;
  url: string;
  command: string;
  argsText: string;
}

const EMPTY_MCP_FORM: McpFormState = {
  originalName: null,
  name: "",
  transport: "http",
  description: "",
  url: "",
  command: "",
  argsText: "",
};

function mcpFormFromServer(server: McpServerSummary): McpFormState {
  return {
    originalName: server.name,
    name: server.name,
    transport: server.transport === "stdio" ? "stdio" : "http",
    description: server.description ?? "",
    url: server.url ?? "",
    command: server.command ?? "",
    argsText: (server.args ?? []).join("\n"),
  };
}

function payloadFromMcpForm(form: McpFormState): UpsertMcpServerInput {
  const base = {
    name: form.name.trim(),
    transport: form.transport,
    description: form.description.trim() || null,
  };
  if (form.transport === "http") {
    return {
      ...base,
      transport: "http",
      url: form.url.trim(),
      command: null,
      args: [],
    };
  }
  return {
    ...base,
    transport: "stdio",
    url: null,
    command: form.command.trim(),
    args: form.argsText
      .split("\n")
      .map((arg) => arg.trim())
      .filter(Boolean),
  };
}

function validateMcpForm(form: McpFormState): string | null {
  const name = form.name.trim();
  if (!name) return "Name is required.";
  if (!/^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}$/.test(name)) {
    return "Name can use letters, numbers, dots, dashes, and underscores.";
  }
  if (form.transport === "http") {
    const url = form.url.trim();
    if (!url) return "URL is required.";
    if (!url.startsWith("http://") && !url.startsWith("https://")) {
      return "URL must start with http:// or https://.";
    }
  }
  if (form.transport === "stdio" && !form.command.trim()) {
    return "Command is required.";
  }
  return null;
}

function McpServerManagerDialog({
  sessionId,
  servers,
  open,
  onOpenChange,
}: {
  sessionId: string;
  servers: McpServerSummary[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [form, setForm] = useState<McpFormState>(EMPTY_MCP_FORM);
  const [formError, setFormError] = useState<string | null>(null);
  const createServer = useCreateMcpServer(sessionId);
  const updateServer = useUpdateMcpServer(sessionId);
  const deleteServer = useDeleteMcpServer(sessionId);
  const mutationError =
    createServer.error?.message ?? updateServer.error?.message ?? deleteServer.error?.message;
  const saving = createServer.isPending || updateServer.isPending;

  function resetForm() {
    setForm(EMPTY_MCP_FORM);
    setFormError(null);
  }

  function handleSave() {
    const error = validateMcpForm(form);
    if (error) {
      setFormError(error);
      return;
    }
    const payload = payloadFromMcpForm(form);
    setFormError(null);
    if (form.originalName) {
      updateServer.mutate(
        { serverName: form.originalName, payload },
        {
          onSuccess: resetForm,
        },
      );
      return;
    }
    createServer.mutate(payload, { onSuccess: resetForm });
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next);
        if (!next) resetForm();
      }}
    >
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Manage MCP Servers</DialogTitle>
          <DialogDescription>Add, edit, or remove MCP servers for this session.</DialogDescription>
        </DialogHeader>
        <div className="grid gap-4 pt-1 md:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
          <div className="flex min-w-0 flex-col gap-1.5">
            <SectionLabel>Servers</SectionLabel>
            {servers.length > 0 ? (
              <div className="flex max-h-56 flex-col divide-y divide-border overflow-y-auto rounded border border-border">
                {servers.map((server) => (
                  <div key={server.name} className="flex min-w-0 items-center gap-1.5 px-2 py-2">
                    <ServerIcon className="size-3.5 shrink-0 text-muted-foreground" />
                    <button
                      type="button"
                      onClick={() => {
                        setForm(mcpFormFromServer(server));
                        setFormError(null);
                      }}
                      className="min-w-0 flex-1 text-left"
                    >
                      <span className="block truncate font-mono text-xs">{server.name}</span>
                      <span className="block truncate text-[11px] text-muted-foreground">
                        {server.transport}
                      </span>
                    </button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-xs"
                      aria-label={`Edit ${server.name}`}
                      onClick={() => {
                        setForm(mcpFormFromServer(server));
                        setFormError(null);
                      }}
                    >
                      <PencilIcon className="size-3" />
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-xs"
                      aria-label={`Delete ${server.name}`}
                      onClick={() => deleteServer.mutate(server.name)}
                      disabled={deleteServer.isPending}
                    >
                      <TrashIcon className="size-3 text-destructive" />
                    </Button>
                  </div>
                ))}
              </div>
            ) : (
              <p className="py-3 text-xs text-muted-foreground">No MCP servers</p>
            )}
            {form.originalName && (
              <Button type="button" variant="ghost" size="sm" onClick={resetForm}>
                <PlusIcon className="size-3.5" />
                New server
              </Button>
            )}
          </div>

          <div className="flex min-w-0 flex-col gap-2">
            <SectionLabel>{form.originalName ? "Edit Server" : "New Server"}</SectionLabel>
            <label className="flex flex-col gap-1 text-xs text-muted-foreground">
              Name
              <Input
                value={form.name}
                onChange={(e) => setForm((prev) => ({ ...prev, name: e.target.value }))}
                className="font-mono"
                placeholder="github"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted-foreground">
              Transport
              <select
                value={form.transport}
                onChange={(e) =>
                  setForm((prev) => ({
                    ...prev,
                    transport: e.target.value === "stdio" ? "stdio" : "http",
                  }))
                }
                className="h-8 rounded-lg border border-input bg-background px-2 text-sm text-foreground outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
              >
                <option value="http">HTTP</option>
                <option value="stdio">stdio</option>
              </select>
            </label>
            {form.transport === "http" ? (
              <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                URL
                <Input
                  value={form.url}
                  onChange={(e) => setForm((prev) => ({ ...prev, url: e.target.value }))}
                  placeholder="https://example.com/sse"
                />
              </label>
            ) : (
              <>
                <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                  Command
                  <Input
                    value={form.command}
                    onChange={(e) => setForm((prev) => ({ ...prev, command: e.target.value }))}
                    placeholder="npx"
                  />
                </label>
                <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                  Args
                  <Textarea
                    value={form.argsText}
                    onChange={(e) => setForm((prev) => ({ ...prev, argsText: e.target.value }))}
                    className="min-h-20 font-mono text-xs"
                    placeholder={"-y\n@modelcontextprotocol/server-github"}
                  />
                </label>
              </>
            )}
            <label className="flex flex-col gap-1 text-xs text-muted-foreground">
              Description
              <Input
                value={form.description}
                onChange={(e) => setForm((prev) => ({ ...prev, description: e.target.value }))}
                placeholder="Optional"
              />
            </label>
            {(formError || mutationError) && (
              <div
                role="alert"
                className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
              >
                {formError ?? mutationError}
              </div>
            )}
            <div className="flex justify-end gap-2 pt-1">
              <Button type="button" variant="ghost" size="sm" onClick={resetForm}>
                <XIcon className="size-3.5" />
                Clear
              </Button>
              <Button type="button" size="sm" onClick={handleSave} disabled={saving || validateMcpForm(form) !== null}>
                <SaveIcon className="size-3.5" />
                {saving ? "Saving..." : "Save"}
              </Button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function McpServersSection({
  sessionId,
  servers,
  editable,
}: {
  sessionId?: string | null;
  servers: McpServerSummary[];
  editable: boolean;
}) {
  const [managerOpen, setManagerOpen] = useState(false);
  const canEdit = !!(sessionId && editable);
  const deleteServer = useDeleteMcpServer(canEdit ? sessionId : "");
  const showSection = servers.length > 0 || canEdit;
  if (!showSection) return null;

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between">
        <SectionLabel>Tools</SectionLabel>
        {canEdit && (
          <button
            type="button"
            onClick={() => setManagerOpen(true)}
            className="rounded p-0.5 hover:bg-muted"
            title="Manage MCP servers"
            aria-label="Manage MCP servers"
          >
            <PlusIcon className="size-3 text-muted-foreground" />
          </button>
        )}
      </div>
      {servers.length > 0 ? (
        <McpServerList
          servers={servers}
          onDelete={canEdit ? (name) => deleteServer.mutate(name) : undefined}
        />
      ) : (
        <p className="text-xs text-muted-foreground">No MCP servers</p>
      )}
      {canEdit && (
        <McpServerManagerDialog
          sessionId={sessionId!}
          servers={servers}
          open={managerOpen}
          onOpenChange={setManagerOpen}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Session policies section (user-editable only)
// ---------------------------------------------------------------------------

function SessionPoliciesSection({ sessionId }: { sessionId: string }) {
  const { data: sessionPolicies = [] } = usePolicies(sessionId);
  const { data: registry = [] } = usePolicyRegistry();
  const deletePolicy = useDeletePolicy(sessionId);
  const [addOpen, setAddOpen] = useState(false);

  const userPolicies = sessionPolicies.filter((p) => p.source === "session");
  const registryByHandler = new Map(registry.map((r) => [r.handler, r]));
  const appliedHandlers = new Set(
    sessionPolicies.map((p) => p.handler).filter((h): h is string => h != null),
  );

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between">
        <SectionLabel>Policies</SectionLabel>
        <button
          type="button"
          onClick={() => setAddOpen(true)}
          className="rounded p-0.5 hover:bg-muted"
          title="Add policy"
        >
          <PlusIcon className="size-3 text-muted-foreground" />
        </button>
      </div>
      {userPolicies.length > 0 ? (
        <div className="flex flex-wrap gap-1">
          {userPolicies.map((p) => {
            const description =
              p.description ??
              (p.handler ? registryByHandler.get(p.handler)?.description : undefined);
            return (
              <Popover key={p.id ?? p.name}>
                <PopoverTrigger asChild>
                  <button
                    type="button"
                    className="flex cursor-pointer items-center gap-0.5 rounded-full border border-border bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:bg-muted/80"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <ShieldCheckIcon className="size-2.5 shrink-0" />
                    {p.name}
                  </button>
                </PopoverTrigger>
                <PopoverContent
                  side="top"
                  align="start"
                  className="w-64"
                  onClick={(e) => e.stopPropagation()}
                >
                  <div className="flex flex-col gap-2">
                    <div className="flex items-center gap-1.5">
                      <ShieldCheckIcon className="size-3.5 text-muted-foreground" />
                      <span className="font-medium text-sm">{p.name}</span>
                    </div>
                    {description && <p className="text-xs text-muted-foreground">{description}</p>}
                    <button
                      type="button"
                      onClick={() => p.id && deletePolicy.mutate(p.id)}
                      className="flex items-center gap-1 self-end rounded px-2 py-1 text-xs text-destructive hover:bg-destructive/10"
                    >
                      <TrashIcon className="size-3" />
                      Remove
                    </button>
                  </div>
                </PopoverContent>
              </Popover>
            );
          })}
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">No policies added</p>
      )}
      <AddPolicyDialog
        sessionId={sessionId}
        registry={registry}
        appliedHandlers={appliedHandlers}
        open={addOpen}
        onOpenChange={setAddOpen}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

interface AgentInfoProps {
  /** The bound agent for the active session. Undefined while loading. */
  agent: Agent | undefined;
  /** Session ID — needed to manage user policies. */
  sessionId?: string | null;
}

/**
 * Whether an agent has any tools worth surfacing in the info popover.
 * Always true when a sessionId is provided (policies section is always shown).
 */
export function agentHasInfo(agent: Agent | undefined, sessionId?: string | null): boolean {
  return !!sessionId || (agent?.mcp_servers?.length ?? 0) > 0;
}

/**
 * The agent's tools & policies body, sans trigger.
 *
 * Shared by the desktop header popover ({@link AgentInfoButton}) and the
 * mobile header menu's agent-info dialog.
 */
export function AgentInfoContent({ agent, sessionId }: AgentInfoProps) {
  const servers = agent?.mcp_servers ?? [];
  const mcpEditable = agent?.mcp_servers_editable === true;
  const displayName = agent ? agentDisplayLabel(agent.name) : null;
  const [sessionIdCopied, setSessionIdCopied] = useState(false);
  const copyResetTimeoutRef = useRef<number | null>(null);
  // Cumulative session spend, live from the store (seeded on bind, updated
  // by SSE ``session_usage``). ``null`` when the session is unpriced (no
  // turn priced yet) — omit the row rather than show "$0.00" / "—".
  const sessionCostUsd = useChatStore((s) => s.sessionCostUsd);
  // Per-model usage breakdown, live from the store (seeded on bind, updated
  // by SSE ``session_usage``). ``null`` until usage is first recorded. The
  // popover renders it directly — the frontend derives any aggregate view
  // from this map rather than receiving flat token fields.
  const usageByModel = useChatStore((s) => s.sessionUsageByModel);
  // Session owner (the user_id granted LEVEL_OWNER), so a viewer can see whose
  // session this is — e.g. a chat shared into a workspace group. ``null`` /
  // undefined when permissions are off (single-user) or still loading, in
  // which case the row is omitted rather than showing a placeholder.
  const { data: owner } = useSessionOwner(sessionId ?? null);
  const viewerId = getCurrentUserId();

  useEffect(() => {
    return () => {
      if (copyResetTimeoutRef.current !== null) window.clearTimeout(copyResetTimeoutRef.current);
    };
  }, []);

  async function copySessionId() {
    if (!sessionId) return;
    try {
      await copyText(sessionId);
    } catch (err) {
      console.warn("Failed to copy session ID", err);
      return;
    }
    setSessionIdCopied(true);
    if (copyResetTimeoutRef.current !== null) window.clearTimeout(copyResetTimeoutRef.current);
    copyResetTimeoutRef.current = window.setTimeout(() => setSessionIdCopied(false), 2000);
  }

  return (
    <div className="flex flex-col gap-3">
      {displayName && (
        <div className="flex flex-col gap-0.5">
          <span className="font-medium text-sm">{displayName}</span>
          {agent?.description && (
            <span className="text-xs text-muted-foreground">{agent.description}</span>
          )}
        </div>
      )}
      {sessionId && owner && (
        <div className="flex flex-col gap-1.5">
          <SectionLabel>Owner</SectionLabel>
          <span
            className="truncate font-mono text-xs text-muted-foreground"
            data-testid="agent-info-session-owner"
            title={owner}
          >
            {owner}
            {owner === viewerId && <span className="ml-1 text-muted-foreground/60">(you)</span>}
          </span>
        </div>
      )}
      {sessionId && (
        <div className="flex flex-col gap-1.5">
          <SectionLabel>Session ID</SectionLabel>
          <div className="flex items-center gap-2">
            <code
              className="min-w-0 flex-1 truncate py-1 font-mono text-xs text-muted-foreground"
              data-testid="agent-info-session-id"
              title={sessionId}
            >
              {sessionId}
            </code>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label={sessionIdCopied ? "Copied session ID" : "Copy session ID"}
              data-testid="agent-info-copy-session-id"
              onClick={copySessionId}
              className="shrink-0"
            >
              {sessionIdCopied ? (
                <CheckIcon className="size-3.5" />
              ) : (
                <CopyIcon className="size-3.5" />
              )}
            </Button>
          </div>
        </div>
      )}
      {sessionId && sessionCostUsd != null && (
        <div className="flex flex-col gap-1.5">
          <SectionLabel>Session cost</SectionLabel>
          <span
            className="font-mono text-xs tabular-nums text-muted-foreground"
            data-testid="agent-info-session-cost"
          >
            {formatSessionCostUsd(sessionCostUsd)}
          </span>
        </div>
      )}
      {sessionId && usageByModel != null && Object.keys(usageByModel).length > 0 && (
        <ModelUsageBreakdown usageByModel={usageByModel} />
      )}
      <McpServersSection sessionId={sessionId} servers={servers} editable={mcpEditable} />
      {sessionId && <SessionPoliciesSection sessionId={sessionId} />}
    </div>
  );
}

/**
 * Header info icon revealing the active agent's tools & policies.
 *
 * Desktop-only: on mobile (`< md`) the same content is reached via the
 * header's three-dot menu, which opens {@link AgentInfoContent} in a
 * dialog. Self-hides when the agent has neither tools nor policies.
 */
export function AgentInfoButton({ agent, sessionId }: AgentInfoProps) {
  if (!agentHasInfo(agent, sessionId)) return null;

  return (
    <Popover>
      <Tooltip>
        <TooltipTrigger asChild>
          <PopoverTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label="Agent tools and policies"
              data-testid="agent-info-trigger"
              className="hidden text-muted-foreground hover:text-foreground md:inline-flex"
            >
              <InfoIcon className="size-4" />
            </Button>
          </PopoverTrigger>
        </TooltipTrigger>
        <TooltipContent>Agent tools &amp; policies</TooltipContent>
      </Tooltip>
      <PopoverContent align="end" className="w-80">
        <AgentInfoContent agent={agent} sessionId={sessionId} />
      </PopoverContent>
    </Popover>
  );
}
