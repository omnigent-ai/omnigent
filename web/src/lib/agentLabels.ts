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
 * harness options or pill suffix at all.
 */
export const BRAIN_HARNESS_LABELS: Record<string, string> = {
  // Insertion order IS the fly-out's menu order.
  "claude-sdk": "Claude SDK",
  "openai-agents": "OpenAI Agents SDK",
  codex: "Codex",
  cursor: "Cursor",
  pi: "Pi",
  antigravity: "Antigravity",
  copilot: "Copilot",
};

interface HarnessCatalogWire {
  data?: { id?: string; label?: string }[];
}

async function fetchHarnessLabels(): Promise<Record<string, string>> {
  const res = await authenticatedFetch("/v1/harnesses");
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as HarnessCatalogWire;
  const labels: Record<string, string> = { ...BRAIN_HARNESS_LABELS };
  for (const row of body.data ?? []) {
    if (typeof row.id === "string" && typeof row.label === "string") {
      labels[row.id] = row.label;
    }
  }
  return labels;
}

export function useBrainHarnessLabels(): Record<string, string> {
  const { data } = useQuery({
    queryKey: ["harness-labels"],
    queryFn: fetchHarnessLabels,
    staleTime: 30_000,
  });
  return data ?? BRAIN_HARNESS_LABELS;
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
