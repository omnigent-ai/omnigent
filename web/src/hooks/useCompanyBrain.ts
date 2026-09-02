import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  activateCompanyBrainResources,
  disconnectCompanyBrainConnection,
  getCompanyBrain,
  listCompanyBrainProviders,
  listCompanyBrainResources,
  previewCompanyBrainResources,
  startCompanyBrainOAuth,
  syncCompanyBrainSelection,
  updateCompanyBrainSelection,
  type CompanyBrainProvider,
  type ProviderResource,
} from "@/lib/companyBrainApi";

export const COMPANY_BRAIN_KEY = ["company-brain"] as const;
export const COMPANY_BRAIN_PROVIDERS_KEY = ["company-brain-providers"] as const;

export function useCompanyBrain(enabled = true) {
  return useQuery({
    queryKey: COMPANY_BRAIN_KEY,
    queryFn: getCompanyBrain,
    enabled,
    staleTime: 10_000,
    refetchInterval: 30_000,
  });
}

export function useCompanyBrainProviders() {
  return useQuery({
    queryKey: COMPANY_BRAIN_PROVIDERS_KEY,
    queryFn: listCompanyBrainProviders,
    staleTime: 60_000,
  });
}

export function useCompanyBrainResources(connectionId: string | null) {
  return useQuery({
    queryKey: ["company-brain-resources", connectionId],
    queryFn: () => listCompanyBrainResources(connectionId as string),
    enabled: connectionId !== null,
    staleTime: 30_000,
  });
}

function useRefreshBrainMutation<TInput, TResult>(mutationFn: (input: TInput) => Promise<TResult>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: COMPANY_BRAIN_KEY });
    },
  });
}

export function useStartCompanyBrainOAuth() {
  return useMutation({
    mutationFn: (provider: CompanyBrainProvider) => startCompanyBrainOAuth(provider),
  });
}

export function usePreviewCompanyBrainResources() {
  return useMutation({
    mutationFn: ({
      connectionId,
      resources,
    }: {
      connectionId: string;
      resources: ProviderResource[];
    }) => previewCompanyBrainResources(connectionId, resources),
  });
}

export function useActivateCompanyBrainResources() {
  return useRefreshBrainMutation(
    ({
      connectionId,
      resources,
      rrule,
    }: {
      connectionId: string;
      resources: ProviderResource[];
      rrule: string | null;
    }) => activateCompanyBrainResources(connectionId, resources, rrule),
  );
}

export function useUpdateCompanyBrainSelection() {
  return useRefreshBrainMutation(
    ({
      id,
      input,
    }: {
      id: string;
      input: { state?: "active" | "paused"; rrule?: string | null; timezone?: string };
    }) => updateCompanyBrainSelection(id, input),
  );
}

export function useSyncCompanyBrainSelection() {
  return useRefreshBrainMutation(({ id, retry }: { id: string; retry?: boolean }) =>
    syncCompanyBrainSelection(id, retry),
  );
}

export function useDisconnectCompanyBrainConnection() {
  return useRefreshBrainMutation((id: string) => disconnectCompanyBrainConnection(id));
}
