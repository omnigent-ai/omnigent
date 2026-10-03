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
}

export interface InventorySkill {
  id: string;
  name: string;
  harness: BrandHarness;
}

export interface InventoryPlugin {
  id: string;
  name: string;
  harness: BrandHarness;
  skillCount?: number;
}

export interface HarnessInventoryContext {
  credentials: InventoryCredential[];
  mcps: InventoryMcpServer[];
  skills: InventorySkill[];
  plugins: InventoryPlugin[];
}

export type InventoryAssetKind = "mcps" | "skills";

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

/** Split `plugin:skill` names into per-plugin counts; the rest are plain skills. */
function splitPluginSkills(skills: SkillSummary[]) {
  const plain: string[] = [];
  const plugins = new Map<string, number>();
  for (const { name } of skills) {
    const split = name.indexOf(":");
    if (split > 0) {
      const plugin = name.slice(0, split);
      plugins.set(plugin, (plugins.get(plugin) ?? 0) + 1);
    } else {
      plain.push(name);
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
        source: gateway ? "Databricks Unity Gateway" : "Signed in",
      });
    }
    const { plain, plugins } = splitPluginSkills(skillsByHarness[harness] ?? []);
    for (const name of plain) context.skills.push({ id: `${harness}:${name}`, name, harness });
    const mcps = mcpServers.filter((server) => server.harness === harness);
    for (const server of mcps) {
      if (server.plugin && !plugins.has(server.plugin)) plugins.set(server.plugin, 0);
      const detail = [server.plugin && `${server.plugin} plugin`, server.url_host]
        .filter(Boolean)
        .join(" · ");
      context.mcps.push({
        id: `${harness}:${server.plugin ? `plugin:${server.plugin}:` : ""}${server.name}`,
        name: server.name,
        harness,
        detail: detail || undefined,
      });
    }
    for (const [name, count] of plugins) {
      context.plugins.push({
        id: `${harness}:${name}`,
        name,
        harness,
        skillCount: count > 0 ? count : undefined,
      });
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
  /**
   * Report a host that's offline, unlisted, or answering 409 as still
   * loading, for a host that's expected to connect shortly.
   */
  awaitConnection?: boolean;
}

/** Discover what each harness on *host* carries into Omnigent sessions. */
export function useHarnessInventory(
  host: Host | null | undefined,
  { enabled = true, awaitConnection = false }: HarnessInventoryOptions = {},
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

  const loading = online && (mcpQuery.isPending || skills.pending);
  const mcpData = mcpQuery.data;
  const skillData = skills.data;
  const context = useMemo(() => {
    if (!online) return EMPTY_CONTEXT;
    const skillsByHarness: Partial<Record<BrandHarness, SkillSummary[]>> = {};
    harnesses.forEach((harness, index) => {
      skillsByHarness[harness] = skillData[index];
    });
    const servers = (mcpData ?? []).filter((server) => isBrandHarness(server.harness));
    return buildInventoryContext(host, harnesses, skillsByHarness, servers);
  }, [online, host, harnesses, mcpData, skillData]);

  const unavailable: InventoryAssetKind[] = [];
  if (online && mcpQuery.isError) unavailable.push("mcps");
  if (online && skills.failed) unavailable.push("skills");
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
