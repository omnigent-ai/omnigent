import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

export interface ExecutionMachine {
  id: string;
  label: string;
}
export interface ExecutionAccount {
  id: string;
  label: string;
  provider: "anthropic" | "openai";
  harness: "claude-native" | "codex-native";
}
/** Identity is (host_id, harness); host_id remains the legacy API route. */
export interface ExecutionBinding {
  host_id: string;
  machine_id: string;
  account_id: string;
  harness: ExecutionAccount["harness"];
  available: boolean;
  reason: string | null;
}
export interface ExecutionTopology {
  configured: boolean;
  machines: ExecutionMachine[];
  accounts: ExecutionAccount[];
  bindings: ExecutionBinding[];
}

const UNCONFIGURED: ExecutionTopology = {
  configured: false,
  machines: [],
  accounts: [],
  bindings: [],
};

/** A server without the endpoint (404) has no registry. Other failures throw; callers treat them as none. */
export async function fetchExecutionTopology(): Promise<ExecutionTopology> {
  const response = await authenticatedFetch("/v1/execution-topology");
  if (response.status === 404) return UNCONFIGURED;
  if (!response.ok) throw new Error("Couldn't load machines and accounts. Try again.");
  return response.json() as Promise<ExecutionTopology>;
}

const LIVE_REFRESH_MS = 7_000;

/**
 * Whether a registry exists only changes on a server restart, so by default
 * it is read once. ``live`` also tracks route availability: it refetches on
 * mount and polls while ``enabled``.
 */
export function useExecutionTopology(enabled: boolean, { live = false }: { live?: boolean } = {}) {
  return useQuery({
    queryKey: ["execution-topology"],
    queryFn: fetchExecutionTopology,
    enabled,
    staleTime: live ? 0 : Infinity,
    refetchOnMount: live ? "always" : true,
    refetchInterval: (query) =>
      live && enabled && query.state.data?.configured ? LIVE_REFRESH_MS : false,
    retry: false,
  });
}

/** Preserve the exact binding, then the machine, then declared available order. */
export function chooseBinding(
  bindings: readonly ExecutionBinding[],
  accountId: string,
  currentHostId: string | null | undefined,
  currentHarness: string | null | undefined,
  currentMachineId: string | null | undefined,
  machineId?: string,
): ExecutionBinding | null {
  const choices = bindings.filter(
    (b) => b.account_id === accountId && b.available && (!machineId || b.machine_id === machineId),
  );
  return (
    choices.find((b) => b.host_id === currentHostId && b.harness === currentHarness) ??
    choices.find((b) => b.machine_id === currentMachineId) ??
    choices[0] ??
    null
  );
}
