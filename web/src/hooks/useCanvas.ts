import { useQuery } from "@tanstack/react-query";
import { getCanvas, type Canvas } from "@/lib/canvasApi";

/**
 * Fetch the conversation's canvas (or null if none). Polls so a canvas the
 * agent sets via set_canvas appears without a manual refresh.
 */
export function useCanvas(conversationId: string | undefined) {
  return useQuery<Canvas | null>({
    queryKey: ["canvas", conversationId ?? null],
    queryFn: () => getCanvas(conversationId as string),
    enabled: !!conversationId,
    staleTime: 5_000,
    refetchInterval: 10_000,
  });
}
