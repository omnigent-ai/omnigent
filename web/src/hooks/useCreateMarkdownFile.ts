import { useCallback, useRef } from "react";
import { toast } from "sonner";

import { useWorkspaceAllFiles } from "@/hooks/useWorkspaceChangedFiles";
import { useCreateFileContent } from "@/hooks/useWriteFileContent";

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
 * Uses the create-only filesystem endpoint and the same ``openFile`` callback
 * the rail uses to show any path. The action stays disabled until the root
 * listing is available, so a missing listing can never fall back to an
 * occupied filename.
 */
export function useCreateMarkdownFile(
  conversationId: string,
  openFile: (path: string) => void,
): { create: () => void; disabled: boolean } {
  const allFiles = useWorkspaceAllFiles(conversationId);
  const createFile = useCreateFileContent(conversationId);
  const creatingRef = useRef(false);
  const ready = allFiles.isSuccess && allFiles.data?.available === true;
  const create = useCallback(() => {
    if (!ready || createFile.isPending || creatingRef.current) return;
    // Treat every root entry as occupied: a directory named untitled.md also
    // prevents creating a file at that path.
    const rootNames = (allFiles.data?.data ?? [])
      .filter((entry) => !entry.path.includes("/"))
      .map((entry) => entry.name);
    const path = newMarkdownFileName(rootNames);
    creatingRef.current = true;
    createFile.mutate(
      { path, content: "" },
      {
        onSuccess: () => {
          creatingRef.current = false;
          openFile(path);
        },
        onError: (error) => {
          creatingRef.current = false;
          toast.error(`Couldn't create ${path}: ${error.message}`);
        },
      },
    );
  }, [allFiles.data?.data, createFile, openFile, ready]);
  return { create, disabled: !ready || createFile.isPending };
}
