import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

/**
 * Shared display-name helpers for agents and brain harnesses, used by
 * both composers (the new-chat landing picker and the in-session chat
 * picker) so the two surfaces can't drift on capitalization or wording.
 */

/**
 * Brain harnesses offered as a per-session override on bundle agents
 * (executor.type: omnigent — polly, debby, and other YAML agents). Keys
 * are canonical server harness ids, values are picker labels. Native
 * terminal wrappers (claude-native / codex-native) are deliberately
 * absent: an agent whose declared harness isn't in this map gets no
 * harness options or pill suffix at all. ``openai-agents`` is likewise
 * omitted — it stays a valid harness for YAML specs (the server
 * ``harness_labels`` catalog drops it too), but is not offered as a pick.
 */
export const BRAIN_HARNESS_LABELS: Record<string, string> = {
  // Insertion order IS the fly-out's menu order.
  "claude-sdk": "Claude SDK",
  codex: "Codex",
  cursor: "Cursor",
  pi: "Pi",
  antigravity: "Antigravity",
  copilot: "Copilot",
};

/** One raw setup step from the server's ``/v1/harnesses`` catalog. */
export interface SetupStepWire {
  kind: string;
  title: string;
  detail: string;
  action: string;
  command: string | null;
  status_key: string | null;
}

interface HarnessCatalogRow {
  id?: string;
  label?: string;
}

interface HarnessCatalogWire {
  data?: HarnessCatalogRow[];
  // Setup steps keyed by EVERY harness spelling a session may declare (native
  // wrappers + installable non-picker ids), not just the picker rows in `data`.
  setup_steps?: Record<string, SetupStepWire[]>;
}

interface HarnessCatalog {
  /** harness id → picker label, merged over the built-in defaults. */
  labels: Record<string, string>;
  /** harness spelling → ordered setup steps (install/auth) the server describes. */
  setupSteps: Record<string, SetupStepWire[]>;
}

async function fetchHarnessCatalog(): Promise<HarnessCatalog> {
  const res = await authenticatedFetch("/v1/harnesses");
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as HarnessCatalogWire;
  const labels: Record<string, string> = { ...BRAIN_HARNESS_LABELS };
  for (const row of body.data ?? []) {
    if (typeof row.id === "string" && typeof row.label === "string") {
      labels[row.id] = row.label;
    }
  }
  // The server keys setup_steps by every spelling (codex-native, opencode, …)
  // so the dialog resolves whatever harness the session declares.
  const setupSteps =
    body.setup_steps && typeof body.setup_steps === "object" ? body.setup_steps : {};
  return { labels, setupSteps };
}

// Both hooks share one request + cache entry, each selecting its own slice.
function useHarnessCatalog<T>(select: (c: HarnessCatalog) => T, fallback: T): T {
  const { data } = useQuery({
    queryKey: ["harness-labels"],
    queryFn: fetchHarnessCatalog,
    staleTime: 30_000,
    select,
  });
  return data ?? fallback;
}

/**
 * Sentinel value sent as ``harness_override`` when the user picks "auto".
 * The server resolves it to a real harness + model via the intelligent router
 * and never persists this string literal.
 */
export const AUTO_HARNESS_ID = "auto";

export function useBrainHarnessLabels(smartRoutingEnabled = false): Record<string, string> {
  const base = useHarnessCatalog((c) => c.labels, BRAIN_HARNESS_LABELS);
  if (!smartRoutingEnabled) return base;
  // Prepend the "auto" sentinel so it appears first in the picker.
  return { [AUTO_HARNESS_ID]: "Auto", ...base };
}

const _NO_SETUP_STEPS: Record<string, SetupStepWire[]> = {};

/** harness id → the server's ordered setup steps (for the setup dialog). */
export function useHarnessSetupSteps(): Record<string, SetupStepWire[]> {
  return useHarnessCatalog((c) => c.setupSteps, _NO_SETUP_STEPS);
}

/**
 * Capitalize the first letter of an agent name for display, e.g.
 * ``"polly"`` → ``"Polly"``. Server agent names are lowercase slugs;
 * both composers show them capital-first.
 */
export function capitalizeAgentName(name: string): string {
  if (name.length === 0) return name;
  return name.charAt(0).toUpperCase() + name.slice(1);
}
