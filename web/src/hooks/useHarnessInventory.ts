// What each harness on a user-connected host already brings to Omnigent: login
// state, user-level skills, plugins, and MCP servers. Read-only; nothing here
// changes the host's configuration.

import { skipToken, useQueries, useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { BRAND_HARNESSES, type BrandHarness } from "@/components/onboarding/harnessBrand";
import type { Host } from "@/hooks/useHosts";
import { fetchSkills, skillsQueryKey, type SkillsTarget } from "@/hooks/useSkills";
import { authenticatedFetch } from "@/lib/identity";
import { ApiError } from "@/lib/sessionsApi";
import type { SkillSummary } from "@/lib/types";

/** The native spelling each brand family reports readiness and skills under. */
export const INVENTORY_HARNESS_IDS: Record<BrandHarness, string> = {
  claude: "claude-native",
  codex: "codex-native",
  cursor: "cursor-native",
};

export interface InventoryCredential {
  harness: BrandHarness;
  /** Where the login comes from, or "Signed in" when the host doesn't say. */
  source: string;
}

export interface InventoryMcpServer {
  id: string;
  name: string;
  harness: BrandHarness;
  /** Secondary label, e.g. the bundling plugin or a remote server's hostname. */
  detail?: string;
  /** Plugin that bundles the server, e.g. ``"figma"``. */
  plugin?: string;
}

export interface InventorySkill {
  id: string;
  name: string;
  harness: BrandHarness;
  description?: string;
}

export interface InventoryPlugin {
  id: string;
  name: string;
  harness: BrandHarness;
  /** Its skill names, without the ``plugin:`` prefix. */
  skills: string[];
  marketplace?: string;
  version?: string | null;
  description?: string | null;
  enabled?: boolean;
  mcp_servers?: string[];
  has_hooks?: boolean;
  has_commands?: boolean;
}

export interface HarnessInventoryContext {
  credentials: InventoryCredential[];
  mcps: InventoryMcpServer[];
  skills: InventorySkill[];
  plugins: InventoryPlugin[];
}

export type InventoryAssetKind = "mcps" | "skills" | "plugins";

export type HarnessInventoryStatus = "loading" | "offline" | "ready";

export interface HarnessInventory {
  status: HarnessInventoryStatus;
  context: HarnessInventoryContext;
  /** Asset kinds the host couldn't report; what did load is still in `context`. */
  unavailable: InventoryAssetKind[];
  /** No MCP servers, skills, or plugins (credentials alone don't count). */
  isEmpty: boolean;
}

/** Wire shape of `GET /v1/hosts/{host_id}/mcp-servers`. */
interface McpServerWire {
  name: string;
  harness: string;
  transport: "stdio" | "http";
  scope: "user";
  plugin?: string | null;
  url_host?: string | null;
}

async function fetchMcpServers(hostId: string, signal: AbortSignal): Promise<McpServerWire[]> {
  const response = await authenticatedFetch(`/v1/hosts/${encodeURIComponent(hostId)}/mcp-servers`, {
    signal,
  });
  if (!response.ok) {
    throw new ApiError(`${response.status} ${response.statusText}`, response.status, null);
  }
  const body = (await response.json()) as { mcp_servers?: McpServerWire[] };
  if (!Array.isArray(body.mcp_servers)) throw new Error("Invalid host MCP servers response");
  return body.mcp_servers;
}

type PluginWire = Omit<InventoryPlugin, "id"> & { marketplace: string };

async function fetchPlugins(hostId: string, signal: AbortSignal): Promise<PluginWire[]> {
  const response = await authenticatedFetch(`/v1/hosts/${encodeURIComponent(hostId)}/plugins`, {
    signal,
  });
  if (!response.ok)
    throw new ApiError(`${response.status} ${response.statusText}`, response.status, null);
  const body = (await response.json()) as { plugins?: PluginWire[] };
  if (!Array.isArray(body.plugins)) throw new Error("Invalid host plugins response");
  return body.plugins;
}

/** Harness families installed on the host; every family when readiness is unknown. */
export function installedHarnesses(host: Host): BrandHarness[] {
  const configured = host.configured_harnesses;
  if (!configured) return [...BRAND_HARNESSES];
  return BRAND_HARNESSES.filter((harness) => {
    const readiness = configured[INVENTORY_HARNESS_IDS[harness]];
    return readiness !== undefined && readiness !== "binary-missing";
  });
}

function isBrandHarness(value: string): value is BrandHarness {
  return (BRAND_HARNESSES as readonly string[]).includes(value);
}

/** Split `plugin:skill` names into per-plugin skill names; the rest are plain skills. */
function splitPluginSkills(skills: SkillSummary[]) {
  const plain: SkillSummary[] = [];
  const plugins = new Map<string, string[]>();
  for (const skill of skills) {
    const split = skill.name.indexOf(":");
    if (split > 0) {
      const plugin = skill.name.slice(0, split);
      plugins.set(plugin, [...(plugins.get(plugin) ?? []), skill.name.slice(split + 1)]);
    } else {
      plain.push(skill);
    }
  }
  return { plain, plugins };
}

/** Assemble the per-harness context from the host's readiness, skills, and MCPs. */
export function buildInventoryContext(
  host: Host,
  harnesses: BrandHarness[],
  skillsByHarness: Partial<Record<BrandHarness, SkillSummary[]>>,
  mcpServers: McpServerWire[],
): HarnessInventoryContext {
  const context: HarnessInventoryContext = { credentials: [], mcps: [], skills: [], plugins: [] };
  for (const harness of harnesses) {
    const id = INVENTORY_HARNESS_IDS[harness];
    if (host.configured_harnesses?.[id] === true) {
      const gateway = host.gateway_inference?.[id] === true;
      context.credentials.push({
        harness,
        source: gateway ? "Databricks AI Gateway" : "Signed in",
      });
    }
    const { plain, plugins } = splitPluginSkills(skillsByHarness[harness] ?? []);
    for (const { name, description } of plain) {
      context.skills.push({ id: `${harness}:${name}`, name, harness, description });
    }
    const mcps = mcpServers.filter((server) => server.harness === harness);
    for (const server of mcps) {
      if (server.plugin && !plugins.has(server.plugin)) plugins.set(server.plugin, []);
      const detail = [server.plugin && `${server.plugin} plugin`, server.url_host]
        .filter(Boolean)
        .join(" · ");
      context.mcps.push({
        id: `${harness}:${server.plugin ? `plugin:${server.plugin}:` : ""}${server.name}`,
        name: server.name,
        harness,
        detail: detail || undefined,
        plugin: server.plugin ?? undefined,
      });
    }
    for (const [name, skills] of plugins) {
      context.plugins.push({ id: `${harness}:${name}`, name, harness, skills });
    }
  }
  return context;
}

/** The server answers 409 while the host's tunnel isn't connected yet. */
function isHostAbsent(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409;
}

// A host listed online can still be registering its tunnel; retry for about a minute.
const CONNECTING_RETRIES = 30;
const CONNECTING_RETRY_MS = 2_000;

function retryWhileConnecting(failureCount: number, error: unknown): boolean {
  return isHostAbsent(error) && failureCount < CONNECTING_RETRIES;
}

const EMPTY_CONTEXT: HarnessInventoryContext = {
  credentials: [],
  mcps: [],
  skills: [],
  plugins: [],
};

interface HarnessInventoryOptions {
  enabled?: boolean;
  /** Settings includes installed-but-disabled plugins; import review shows active assets. */
  includePluginMetadata?: boolean;
  /**
   * Report a host that's offline, unlisted, or answering 409 as still
   * loading, for a host that's expected to connect shortly.
   */
  awaitConnection?: boolean;
}

/** Discover what each harness on *host* carries into Omnigent sessions. */
export function useHarnessInventory(
  host: Host | null | undefined,
  {
    enabled = true,
    awaitConnection = false,
    includePluginMetadata = false,
  }: HarnessInventoryOptions = {},
): HarnessInventory {
  const retry = awaitConnection ? retryWhileConnecting : false;
  const online = enabled && host != null && host.status === "online";
  const harnesses = useMemo(() => (online ? installedHarnesses(host) : []), [online, host]);
  const skills = useQueries({
    queries: harnesses.map((harness) => {
      const target: SkillsTarget = {
        hostId: host?.host_id ?? "",
        harness: INVENTORY_HARNESS_IDS[harness],
        path: "~",
      };
      return {
        queryKey: skillsQueryKey(target),
        queryFn: ({ signal }: { signal: AbortSignal }) => fetchSkills(target, signal),
        staleTime: 30_000,
        retry,
        retryDelay: CONNECTING_RETRY_MS,
      };
    }),
    // Structurally shared, so `data` keeps its identity until a catalog changes.
    combine: (results) => ({
      data: results.map((result) => result.data),
      pending: results.some((result) => result.isPending),
      failed: results.some((result) => result.isError),
    }),
  });
  const mcpQuery = useQuery({
    queryKey: ["host-mcp-servers", host?.host_id],
    queryFn: online ? ({ signal }) => fetchMcpServers(host.host_id, signal) : skipToken,
    staleTime: 30_000,
    retry,
    retryDelay: CONNECTING_RETRY_MS,
  });

  const pluginsQuery = useQuery({
    queryKey: ["host-plugins", host?.host_id],
    queryFn:
      online && includePluginMetadata
        ? ({ signal }) => fetchPlugins(host.host_id, signal)
        : skipToken,
    staleTime: 30_000,
    retry,
    retryDelay: CONNECTING_RETRY_MS,
  });
  const legacyPlugins =
    !includePluginMetadata ||
    (pluginsQuery.error instanceof ApiError &&
      (pluginsQuery.error.status === 404 || pluginsQuery.error.status === 501));
  const pluginData = pluginsQuery.data;
  const loading =
    online &&
    (mcpQuery.isPending || skills.pending || (includePluginMetadata && pluginsQuery.isPending));
  const mcpData = mcpQuery.data;
  const skillData = skills.data;
  const context = useMemo(() => {
    if (!online) return EMPTY_CONTEXT;
    const skillsByHarness: Partial<Record<BrandHarness, SkillSummary[]>> = {};
    harnesses.forEach((harness, index) => {
      skillsByHarness[harness] = skillData[index];
    });
    const servers = (mcpData ?? []).filter((server) => isBrandHarness(server.harness));
    const assembled = buildInventoryContext(host, harnesses, skillsByHarness, servers);
    // The metadata endpoint covers Claude; other families keep their existing inventory.
    if (!legacyPlugins) {
      assembled.plugins = assembled.plugins.filter((plugin) => plugin.harness !== "claude");
      for (const plugin of pluginData ?? []) {
        if (plugin.harness !== "claude" || !harnesses.includes("claude")) continue;
        assembled.plugins.push({ ...plugin, id: `claude:${plugin.name}@${plugin.marketplace}` });
      }
    }
    return assembled;
  }, [online, host, harnesses, mcpData, skillData, pluginData, legacyPlugins]);

  const unavailable: InventoryAssetKind[] = [];
  if (online && mcpQuery.isError) unavailable.push("mcps");
  if (online && skills.failed) unavailable.push("skills");
  if (
    online &&
    includePluginMetadata &&
    (legacyPlugins ? mcpQuery.isError && skills.failed : pluginsQuery.isError)
  ) {
    unavailable.push("plugins");
  }
  return {
    status: !online
      ? enabled && awaitConnection
        ? "loading"
        : "offline"
      : loading
        ? "loading"
        : "ready",
    context,
    unavailable,
    isEmpty: context.mcps.length + context.skills.length + context.plugins.length === 0,
  };
}
