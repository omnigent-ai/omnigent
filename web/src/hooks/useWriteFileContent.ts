import { useMutation, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import type { FileContentResponse } from "./useFileContent";

const DEFAULT_ENVIRONMENT_ID = "default";

async function writeFileContent(
  conversationId: string,
  path: string,
  content: string,
): Promise<void> {
  const encodedPath = path.split("/").map(encodeURIComponent).join("/");
  const url =
    `/v1/sessions/${encodeURIComponent(conversationId)}` +
    `/resources/environments/${DEFAULT_ENVIRONMENT_ID}/filesystem/${encodedPath}`;
  const res = await authenticatedFetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, encoding: "utf-8" }),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
}

/**
 * Write the content of a workspace file for the given conversation.
 * Invalidates the file-content query on success so the viewer refreshes.
 */
export function useWriteFileContent(conversationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ path, content }: { path: string; content: string }) =>
      writeFileContent(conversationId, path, content),
    onSuccess: (_, { path, content }) => {
      queryClient.setQueryData<FileContentResponse>(
        ["file-content", conversationId, path],
        (old) => (old ? { ...old, content } : undefined),
      );
      queryClient.invalidateQueries({ queryKey: ["file-content", conversationId, path] });
      queryClient.invalidateQueries({ queryKey: ["workspace-changed-files", conversationId] });
    },
  });
}
