// React-query hooks for the host provider / agent-spec pin endpoints.
// Query keys are namespaced per host so switching hosts in the section
// refetches rather than cross-contaminating caches.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  clearHostAgentPin,
  deleteHostProvider,
  fetchHostAgentSpecs,
  fetchHostProviders,
  pinHostAgent,
  testHostProvider,
  upsertHostProvider,
  type AgentPinInput,
  type HostProvider,
  type ProviderTestResult,
} from "@/lib/hostProvidersApi";

export function useHostProviders(hostId: string | null) {
  return useQuery({
    queryKey: ["host-providers", hostId],
    queryFn: () => fetchHostProviders(hostId as string),
    enabled: hostId !== null,
  });
}

export function useHostAgentSpecs(hostId: string | null) {
  return useQuery({
    queryKey: ["host-agent-specs", hostId],
    queryFn: () => fetchHostAgentSpecs(hostId as string),
    enabled: hostId !== null,
  });
}

function useInvalidateHostConfig(hostId: string | null) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ["host-providers", hostId] });
    void queryClient.invalidateQueries({ queryKey: ["host-agent-specs", hostId] });
  };
}

export function useUpsertHostProvider(hostId: string | null) {
  const invalidate = useInvalidateHostConfig(hostId);
  return useMutation({
    mutationFn: ({ name, entry }: { name: string; entry: Record<string, unknown> }) =>
      upsertHostProvider(hostId as string, name, entry),
    onSuccess: invalidate,
  });
}

export function useDeleteHostProvider(hostId: string | null) {
  const invalidate = useInvalidateHostConfig(hostId);
  return useMutation({
    mutationFn: (name: string) => deleteHostProvider(hostId as string, name),
    onSuccess: invalidate,
  });
}

export function useTestHostProvider(hostId: string | null) {
  return useMutation({
    mutationFn: (name: string): Promise<ProviderTestResult> =>
      testHostProvider(hostId as string, name),
  });
}

export function usePinHostAgent(hostId: string | null) {
  const invalidate = useInvalidateHostConfig(hostId);
  return useMutation({
    mutationFn: ({ agent, pin }: { agent: string; pin: AgentPinInput }) =>
      pinHostAgent(hostId as string, agent, pin),
    onSuccess: invalidate,
  });
}

export function useClearHostAgentPin(hostId: string | null) {
  const invalidate = useInvalidateHostConfig(hostId);
  return useMutation({
    mutationFn: (agent: string) => clearHostAgentPin(hostId as string, agent),
    onSuccess: invalidate,
  });
}

export type { HostProvider };
