import { FileCode2Icon, Loader2Icon, RefreshCwIcon } from "lucide-react";
import { useMemo } from "react";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import { useWorkspaceFileSearch } from "@/hooks/useWorkspaceChangedFiles";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export interface ArtifactEntry {
  entryPath: string;
  title: string;
  modifiedAt: number | null;
}

function titleFromSlug(slug: string): string {
  return slug
    .split(/[-_]/g)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function artifactEntriesFromFiles(files: WorkspaceFile[]): ArtifactEntry[] {
  const entries: ArtifactEntry[] = [];
  for (const file of files) {
    if (file.type !== "file") continue;
    const parts = file.path.split("/");
    const standalone =
      parts.length === 2 && parts[0] === "artifacts" && parts[1]!.toLowerCase().endsWith(".html");
    const directoryEntry =
      parts.length === 3 && parts[0] === "artifacts" && parts[2]!.toLowerCase() === "index.html";
    if (!standalone && !directoryEntry) continue;
    const slug = standalone ? parts[1]!.slice(0, -5) : parts[1]!;
    entries.push({
      entryPath: file.path,
      title: titleFromSlug(slug),
      modifiedAt: file.modified_at,
    });
  }
  return entries.sort((a, b) => a.entryPath.localeCompare(b.entryPath));
}

interface ArtifactsPanelProps {
  conversationId: string;
  selectedPath: string | null;
  onSelect: (entryPath: string) => void;
  onOpenFile: (entryPath: string) => void;
}

export function ArtifactsPanel({
  conversationId,
  selectedPath,
  onSelect,
  onOpenFile,
}: ArtifactsPanelProps) {
  const query = useWorkspaceFileSearch(conversationId, ".html", "artifacts/**");
  const entries = useMemo(() => artifactEntriesFromFiles(query.data ?? []), [query.data]);
  const selected = entries.find((entry) => entry.entryPath === selectedPath) ?? null;

  if (query.isLoading) {
    return (
      <div className="flex flex-1 items-center justify-center gap-2 text-sm text-muted-foreground">
        <Loader2Icon className="size-4 animate-spin" />
        Loading artifacts…
      </div>
    );
  }

  if (query.isError) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center">
        <p className="text-sm font-medium">Artifacts are unavailable</p>
        <p className="text-xs text-muted-foreground">
          The agent may be asleep or its workspace may still be starting.
        </p>
        <Button variant="outline" size="sm" onClick={() => void query.refetch()}>
          <RefreshCwIcon className="size-3.5" />
          Retry
        </Button>
      </div>
    );
  }

  if (entries.length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center">
        <FileCode2Icon className="size-8 text-muted-foreground/60" />
        <p className="text-sm font-medium">No design artifacts yet</p>
        <p className="text-xs text-muted-foreground">
          Willy publishes reviewed HTML entry points from the artifacts folder.
        </p>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="border-b border-border px-3 py-2">
        <p className="text-xs font-medium text-muted-foreground">Design artifacts</p>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        <div className="space-y-1">
          {entries.map((entry) => (
            <button
              key={entry.entryPath}
              type="button"
              onClick={() => onSelect(entry.entryPath)}
              className={cn(
                "flex w-full items-center gap-2 rounded-md px-2 py-2 text-left hover:bg-muted",
                entry.entryPath === selectedPath && "bg-muted",
              )}
            >
              <FileCode2Icon className="size-4 shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium">{entry.title}</span>
                <span className="block truncate font-mono text-[11px] text-muted-foreground">
                  {entry.entryPath}
                </span>
              </span>
            </button>
          ))}
        </div>
      </div>
      {selected !== null && (
        <div className="border-t border-border p-3">
          <Button
            className="w-full"
            variant="outline"
            size="sm"
            onClick={() => onOpenFile(selected.entryPath)}
          >
            Open file
          </Button>
        </div>
      )}
    </div>
  );
}
