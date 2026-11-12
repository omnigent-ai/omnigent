import { useMutation, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { browseLocationBase, browseLocationSegment } from "@/hooks/useWorkspaceChangedFiles";
import type { FileContentResponse } from "./useFileContent";

const DEFAULT_ENVIRONMENT_ID = "default";

// This tab's writes in flight, per session file. Reads wait for them and a
// write cancels reads already in flight, so no read can return this tab's
// half-finished save as if another writer had changed the file.
const writesInFlight = new Map<string, Set<Promise<void>>>();

function fileWriteKey(conversationId: string, path: string): string {
  return JSON.stringify([conversationId, path]);
}

function trackFileWrite(conversationId: string, path: string, write: Promise<void>): Promise<void> {
  const key = fileWriteKey(conversationId, path);
  const writes = writesInFlight.get(key) ?? new Set<Promise<void>>();
  writes.add(write);
  writesInFlight.set(key, writes);
  const forget = () => {
    writes.delete(write);
    if (writes.size === 0 && writesInFlight.get(key) === writes) writesInFlight.delete(key);
  };
  write.then(forget, forget);
  return write;
}

/**
 * Resolve once this tab has no write in flight for a file.
 *
 * :param conversationId: The session ID, e.g. ``"sess_abc123"``.
 * :param path: Workspace-relative or host-absolute file path, e.g. ``"notes.md"``.
 */
export async function fileWritesSettled(conversationId: string, path: string): Promise<void> {
  const writes = writesInFlight.get(fileWriteKey(conversationId, path));
  if (writes) await Promise.allSettled(writes);
}

async function writeFileContent(
  conversationId: string,
  path: string,
  content: string,
): Promise<void> {
  // Same slash-merge-safe wire form as the read path, via the shared helpers:
  // a bare per-segment-encoded path, and `?base=host` for an absolute location
  // (saving a file edited outside the workspace). See `browseLocationSegment`.
  const encodedPath = browseLocationSegment(path);
  const base = browseLocationBase(path);
  const url =
    `/v1/sessions/${encodeURIComponent(conversationId)}` +
    `/resources/environments/${DEFAULT_ENVIRONMENT_ID}/filesystem/${encodedPath}` +
    (base ? `?base=${base}` : "");
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
    mutationFn: ({ path, content }: { path: string; content: string }) => {
      void queryClient.cancelQueries({ queryKey: ["file-content", conversationId, path] });
      return trackFileWrite(conversationId, path, writeFileContent(conversationId, path, content));
    },
    onSuccess: (_, { path, content }) => {
      queryClient.setQueryData<FileContentResponse>(
        ["file-content", conversationId, path],
        (old) => (old ? { ...old, content } : undefined),
      );
      queryClient.invalidateQueries({ queryKey: ["file-content", conversationId, path] });
      queryClient.invalidateQueries({ queryKey: ["workspace-changed-files", conversationId] });
    },
    // The write cancelled any read in flight; re-read so its result isn't lost.
    onError: (_, { path }) => {
      void queryClient.invalidateQueries({ queryKey: ["file-content", conversationId, path] });
    },
  });
}
