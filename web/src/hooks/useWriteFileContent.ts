import { useMutation, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import type { FileContentResponse } from "./useFileContent";
import { DEFAULT_WORKSPACE_ENVIRONMENT_ID } from "@/lib/workspaceFiles";

const DEFAULT_ENVIRONMENT_ID = DEFAULT_WORKSPACE_ENVIRONMENT_ID;

async function writeFileContent(
  conversationId: string,
  path: string,
  content: string,
  environmentId: string,
): Promise<void> {
  const encodedPath = path.split("/").map(encodeURIComponent).join("/");
  const url =
    `/v1/sessions/${encodeURIComponent(conversationId)}` +
    `/resources/environments/${encodeURIComponent(environmentId)}/filesystem/${encodedPath}`;
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
export function useWriteFileContent(
  conversationId: string,
  environmentId = DEFAULT_ENVIRONMENT_ID,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ path, content }: { path: string; content: string }) =>
      writeFileContent(conversationId, path, content, environmentId),
    onSuccess: (_, { path, content }) => {
      queryClient.setQueryData<FileContentResponse>(
        ["file-content", conversationId, environmentId, path],
        (old) => (old ? { ...old, content } : undefined),
      );
      queryClient.invalidateQueries({
        queryKey: ["file-content", conversationId, environmentId, path],
      });
      queryClient.invalidateQueries({
        queryKey: ["workspace-changed-files", conversationId, environmentId],
      });
    },
  });
}
