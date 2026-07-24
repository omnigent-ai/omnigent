import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

export interface ArtifactPreviewGrant {
  url: string;
  expires_at: number;
}

export function useArtifactPreview(
  conversationId: string,
  entryPath: string | null,
  revision: number | null = null,
) {
  return useQuery({
    queryKey: ["artifact-preview", conversationId, entryPath, revision],
    enabled: entryPath !== null,
    retry: false,
    refetchOnWindowFocus: false,
    queryFn: async (): Promise<ArtifactPreviewGrant> => {
      if (entryPath === null) throw new Error("artifact entry path is required");
      const response = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(conversationId)}/artifact-previews`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ entry_path: entryPath }),
        },
      );
      if (!response.ok) {
        throw new Error(`Artifact preview unavailable (${response.status})`);
      }
      return (await response.json()) as ArtifactPreviewGrant;
    },
  });
}
