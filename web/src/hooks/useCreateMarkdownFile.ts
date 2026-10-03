import { useCallback } from "react";

import { useWorkspaceAllFiles } from "@/hooks/useWorkspaceChangedFiles";
import { useWriteFileContent } from "@/hooks/useWriteFileContent";

/**
 * First free "untitled[-N].md" name at the workspace root, given the existing
 * root file names: "untitled.md", then "untitled-2.md", "untitled-3.md", …
 * (the lowest free index, so a gap is reused).
 */
export function newMarkdownFileName(existingNames: Iterable<string>): string {
  const taken = new Set(existingNames);
  if (!taken.has("untitled.md")) return "untitled.md";
  for (let n = 2; ; n += 1) {
    const candidate = `untitled-${n}.md`;
    if (!taken.has(candidate)) return candidate;
  }
}

/**
 * Returns a callback that creates a new empty markdown file at the workspace
 * root — a unique ``untitled[-N].md`` — and opens it in the file viewer, where
 * a ``.md`` file lands in the rich-text editor with autosave.
 *
 * Reuses the write-file endpoint (which creates a missing file server-side) and
 * the same ``openFile`` the rail uses to show any path. Uniqueness is computed
 * against the workspace root listing; if it hasn't loaded yet the name falls
 * back to ``untitled.md``.
 */
export function useCreateMarkdownFile(
  conversationId: string,
  openFile: (path: string) => void,
): () => void {
  const allFiles = useWorkspaceAllFiles(conversationId);
  const write = useWriteFileContent(conversationId);
  const rootFiles = allFiles.data?.data;
  return useCallback(() => {
    // Only root-level files collide with a root ``untitled.md`` — a nested
    // ``docs/untitled.md`` has a slash in its path and must not count.
    const rootNames = (rootFiles ?? [])
      .filter((f) => f.type === "file" && !f.path.includes("/"))
      .map((f) => f.name);
    const path = newMarkdownFileName(rootNames);
    write.mutate({ path, content: "" }, { onSuccess: () => openFile(path) });
  }, [rootFiles, write, openFile]);
}
