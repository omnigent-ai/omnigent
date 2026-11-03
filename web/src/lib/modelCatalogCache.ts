import { z } from "zod";
import { getOmnigentServerIdentity } from "./host";
import { getCurrentUserId } from "./identity";
import type { NativeModelOption } from "./types";

export const modelOptionSchema = z.object({
  id: z.string(),
  model: z.string().optional(),
  displayName: z.string().optional(),
  isDefault: z.boolean().optional(),
  defaultReasoningEffort: z.string().optional(),
  supportedReasoningEfforts: z
    .array(z.object({ reasoningEffort: z.string(), description: z.string().optional() }))
    .optional(),
  source: z
    .object({
      kind: z.string(),
      label: z.string(),
      name: z.string().optional(),
      host: z.string().optional(),
    })
    .optional(),
});

const MAX_AGE_MS = 24 * 60 * 60 * 1000;
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
  return `omnigent:model-catalog:v1:${JSON.stringify([server, user, target, targetId, harness, target === "sandbox" ? agentId : null])}`;
}

export function readModelCatalogCache(key: string | null) {
  if (key === null || typeof window === "undefined") return null;
  try {
    const result = cacheSchema.safeParse(JSON.parse(window.localStorage.getItem(key) ?? "null"));
    if (!result.success) return null;
    const age = Date.now() - result.data.savedAt;
    return age >= 0 && age <= MAX_AGE_MS ? result.data : null;
  } catch {
    return null;
  }
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
