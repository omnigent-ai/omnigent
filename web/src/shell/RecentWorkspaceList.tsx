import GithubMono from "@lobehub/icons/es/Github/components/Mono";
import { useQueries } from "@tanstack/react-query";
import { ChevronRightIcon, FolderIcon } from "lucide-react";

import { hostWorktreesQueryOptions } from "@/hooks/useHostWorktrees";

export interface RecentWorkspaceListProps {
  hostId: string | null;
  paths: string[];
  onSelect: (path: string) => void;
  onBrowse: (path: string) => void;
}

/**
 * Working-directory recent rows. The heading, divider, and final Open folder
 * action stay with the owning popover so their section spacing is unchanged.
 */
export function RecentWorkspaceList({
  hostId,
  paths,
  onSelect,
  onBrowse,
}: RecentWorkspaceListProps) {
  const worktreeQueries = useQueries({
    queries: paths.map((path) => ({
      ...hostWorktreesQueryOptions(hostId ?? "", path),
      enabled: hostId !== null && path !== "",
    })),
  });

  return (
    <div className="flex flex-col gap-0" data-testid="recent-workspace-list">
      {paths.map((path, index) => {
        const isGithub = worktreeQueries[index]?.data?.[0]?.remote_provider === "github";
        return (
          <div
            key={path}
            className="group/recent flex min-w-0 items-center rounded-md hover:bg-muted focus-within:bg-muted"
            data-testid={`recent-workspace-row-${index}`}
          >
            <button
              type="button"
              className="flex min-w-0 flex-1 items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm"
              onClick={() => onSelect(path)}
              data-testid={`recent-workspace-select-${index}`}
            >
              {isGithub ? (
                <GithubMono
                  size={16}
                  className="shrink-0 text-muted-foreground"
                  aria-hidden
                  data-testid={`recent-workspace-icon-${index}-github`}
                />
              ) : (
                <FolderIcon
                  className="size-4 shrink-0 text-muted-foreground"
                  aria-hidden
                  data-testid={`recent-workspace-icon-${index}-folder`}
                />
              )}
              <span className="truncate">{path}</span>
            </button>
            <button
              type="button"
              className="mr-1 inline-flex size-7 shrink-0 items-center justify-center rounded text-muted-foreground opacity-0 transition hover:bg-background hover:text-foreground focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 group-hover/recent:opacity-100 group-focus-within/recent:opacity-100"
              aria-label={`Browse ${path}`}
              title={`Browse ${path}`}
              onClick={() => onBrowse(path)}
              data-testid={`recent-workspace-browse-${index}`}
            >
              <ChevronRightIcon className="size-4" aria-hidden />
            </button>
          </div>
        );
      })}
    </div>
  );
}
