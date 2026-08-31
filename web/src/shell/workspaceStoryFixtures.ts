import type { QueryClient } from "@tanstack/react-query";
import type { HostFilesystemEntry } from "@/hooks/useHostFilesystem";

export const workspaceStoryHost = "host-workspace-story";
export const workspaceStoryHome = "/Users/story";
export const workspaceStoryProjects = "/Users/story/projects";

export const storyDirectory = (path: string): HostFilesystemEntry => ({
  name: path.split("/").at(-1) ?? path,
  path,
  type: "directory",
  bytes: null,
  modified_at: 1_700_000_000,
});

export const storyFile = (path: string, bytes: number): HostFilesystemEntry => ({
  name: path.split("/").at(-1) ?? path,
  path,
  type: "file",
  bytes,
  modified_at: 1_700_000_000,
});

export function seedFilesystem(
  queryClient: QueryClient,
  path: string,
  entries: HostFilesystemEntry[],
): void {
  queryClient.setQueryData(["host-filesystem", workspaceStoryHost, path], {
    entries,
    truncated: false,
  });
}
