import { isAdminViewEnabled } from "@/lib/adminView";
import { z } from "zod";
import { getOmnigentServerIdentity } from "./host";
import { getCurrentUserId } from "./identity";
import type { NativeModelOption } from "./types";

// Runner-reported catalogs send null for unset fields; read them as absent.
function unset<T extends z.ZodType>(schema: T) {
  return z.preprocess((value) => value ?? undefined, schema.optional());
}

export const modelOptionSchema = z.object({
  id: z.string(),
  model: unset(z.string()),
  displayName: unset(z.string()),
  isDefault: unset(z.boolean()),
  defaultReasoningEffort: unset(z.string()),
  supportedReasoningEfforts: unset(
    z.array(z.object({ reasoningEffort: z.string(), description: unset(z.string()) })),
  ),
  source: unset(
    z.object({
      kind: z.string(),
      label: z.string(),
      name: unset(z.string()),
      host: unset(z.string()),
    }),
  ),
});

const MAX_AGE_MS = 24 * 60 * 60 * 1000;
/** Session catalogs are only a launch hint; the session rechecks its pick once online. */
export const SANDBOX_SESSION_CATALOG_MAX_AGE_MS = 7 * MAX_AGE_MS;
const cacheSchema = z.object({
  models: z.array(modelOptionSchema),
  configured: z.boolean(),
  savedAt: z.number(),
});

export function modelCatalogCacheKey(
  target: "host" | "sandbox",
  targetId: string | null,
  harness: string | null,
  agentId: string | null,
): string | null {
  const server = getOmnigentServerIdentity();
  const user = getCurrentUserId();
  if (server === null || user === null || !targetId || !harness) return null;
  return `omnigent:model-catalog:v1:${JSON.stringify([server, user, target, targetId, harness, target === "sandbox" ? agentId : null, isAdminViewEnabled()])}`;
}

/**
 * Key for the catalog a sandbox session last reported. Scoped by agent, so an
 * agent with its own provider never shares its models with other agents.
 */
export function sandboxSessionCatalogKey(
  provider: string | null,
  harness: string | null,
  agentId: string | null,
): string | null {
  const server = getOmnigentServerIdentity();
  const user = getCurrentUserId();
  if (server === null || user === null || !provider || !harness || !agentId) return null;
  return `omnigent:model-catalog:v1:${JSON.stringify([server, user, "sandbox-session", provider, harness, agentId, isAdminViewEnabled()])}`;
}

/** The raw stored entry, comparable across renders. */
export function readModelCatalogCacheText(key: string | null): string | null {
  if (key === null || typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function parseModelCatalogCache(text: string | null, maxAgeMs = MAX_AGE_MS) {
  if (text === null) return null;
  try {
    const result = cacheSchema.safeParse(JSON.parse(text));
    if (!result.success) return null;
    const age = Date.now() - result.data.savedAt;
    return age >= 0 && age <= maxAgeMs ? result.data : null;
  } catch {
    return null;
  }
}

export function readModelCatalogCache(key: string | null, maxAgeMs = MAX_AGE_MS) {
  return parseModelCatalogCache(readModelCatalogCacheText(key), maxAgeMs);
}

/** Store catalog data only; live queries always own launch availability. */
export function writeModelCatalogCache(
  key: string | null,
  models: readonly NativeModelOption[],
  configured: boolean,
  savedAt = Date.now(),
) {
  if (key === null || typeof window === "undefined") return;
  try {
    // Fusion configurations require a separate editor and cannot be restored as model IDs.
    window.localStorage.setItem(
      key,
      JSON.stringify({
        models: models.filter((model) => !model.fusion),
        configured,
        savedAt,
      }),
    );
  } catch {
    // Unavailable storage must not prevent configuring an automation.
  }
}
