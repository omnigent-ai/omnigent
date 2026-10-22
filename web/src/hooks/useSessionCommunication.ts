import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getOrchestrationSetting, setOrchestrationSetting } from "@/lib/sessionCommunicationApi";

export const SESSION_MESSAGES_KEY = ["session-communication"] as const;
export function useOrchestrationSetting(enabled: boolean) {
  return useQuery({
    queryKey: [...SESSION_MESSAGES_KEY, "setting"],
    queryFn: ({ signal }) => getOrchestrationSetting(signal),
    enabled,
  });
}
export function useChangeOrchestrationSetting() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: setOrchestrationSetting,
    onSuccess: (setting) => client.setQueryData([...SESSION_MESSAGES_KEY, "setting"], setting),
    onError: () => client.invalidateQueries({ queryKey: [...SESSION_MESSAGES_KEY, "setting"] }),
  });
}
