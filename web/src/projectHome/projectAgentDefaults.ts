import type { Agent } from "@/hooks/useAgents";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { Host } from "@/hooks/useHosts";
import { harnessReadinessOnHost } from "@/lib/harnessSetup";
import { nativeCodingAgentForAvailableAgent } from "@/lib/nativeCodingAgents";
import { canonicalNativeHarness, nativePermissionOptions } from "@/lib/nativeHarnessModes";

/** User-created orchestrators start with their harness's highest supported access mode. */
export function projectOrchestratorPermissionMode(harness: string | null | undefined) {
  if (!harness) return undefined;
  const canonical = canonicalNativeHarness(harness);
  // Keep aligned with the server's project_main_session defaults.
  const modes: Readonly<Record<string, string>> = {
    "claude-native": "bypassPermissions",
    "codex-native": "bypass",
    "cursor-native": "yolo",
    "antigravity-native": "skip",
    "devin-native": "dangerous",
  };
  return nativePermissionOptions(canonical).find((mode) => mode.value === modes[canonical])?.value;
}

/** Catalog order and host readiness decide the default; an explicit pick stays explicit. */
export function firstAvailableNativeAgent(agents: readonly AvailableAgent[], host?: Host) {
  return agents.find((agent) => {
    const native = nativeCodingAgentForAvailableAgent(agent);
    return native && (!host || harnessReadinessOnHost(native.harness, host).selectable);
  });
}

/** Project identity follows the selected agent, not the main-session role. */
export function projectAgentDescription(
  agent: Pick<Agent, "name" | "description"> | undefined,
): string | null {
  return agent?.description?.trim() || null;
}
