import { FileCode2Icon, Loader2Icon, RefreshCwIcon, ScanSearchIcon } from "lucide-react";
import { useId, useMemo, useState } from "react";
import { useArtifactPreview } from "@/hooks/useArtifactPreview";
import { useManagedArtifacts } from "@/hooks/useManagedArtifacts";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ArtifactPreviewSurface } from "./ArtifactPreviewSurface";
import { hasNativeArtifactInspector, inspectNativeArtifactSurface } from "@/lib/nativeBridge";

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
}

export function ArtifactsPanel({ conversationId, selectedPath, onSelect }: ArtifactsPanelProps) {
  const query = useManagedArtifacts(conversationId);
  const entries = useMemo(() => artifactEntriesFromFiles(query.data ?? []), [query.data]);
  const selected = entries.find((entry) => entry.entryPath === selectedPath) ?? null;
  const preview = useArtifactPreview(conversationId, selected?.entryPath ?? null);
  const [selectorOpen, setSelectorOpen] = useState(false);
  const [inspecting, setInspecting] = useState(false);
  const artifactSurfaceId = `artifact-surface-${useId()}`;
  const canInspect = hasNativeArtifactInspector();

  const inspectArtifact = async () => {
    setInspecting(true);
    try {
      await inspectNativeArtifactSurface(artifactSurfaceId);
    } finally {
      setInspecting(false);
    }
  };

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
        <p className="text-sm font-medium">No artifacts yet</p>
        <p className="text-xs text-muted-foreground">
          Published HTML artifacts appear here for preview.
        </p>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center border-b border-border px-2 py-1.5">
        <Select value={selected?.entryPath} onValueChange={onSelect} onOpenChange={setSelectorOpen}>
          <SelectTrigger
            aria-label="Select artifact"
            className="h-8 min-w-0 flex-1 justify-start border-0 bg-transparent px-2 shadow-none hover:bg-muted *:data-[slot=select-value]:flex-1 *:data-[slot=select-value]:text-left dark:bg-transparent dark:hover:bg-muted"
          >
            <FileCode2Icon data-icon="inline-start" className="size-4 text-muted-foreground" />
            <SelectValue placeholder="Choose an artifact" />
          </SelectTrigger>
          <SelectContent position="popper" align="start">
            {entries.map((entry) => (
              <SelectItem key={entry.entryPath} value={entry.entryPath}>
                {entry.title}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {canInspect && selected !== null && preview.data !== undefined ? (
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Inspect artifact element"
            title="Inspect artifact element"
            disabled={inspecting}
            onClick={() => void inspectArtifact()}
          >
            {inspecting ? (
              <Loader2Icon className="size-4 animate-spin" />
            ) : (
              <ScanSearchIcon className="size-4" />
            )}
          </Button>
        ) : null}
      </div>
      {selected === null ? (
        <div className="flex min-h-0 flex-1 items-center justify-center px-6 text-center text-sm text-muted-foreground">
          Choose an artifact to preview.
        </div>
      ) : (
        <div className="relative min-h-0 flex-1 bg-muted/30">
          {preview.isLoading ? (
            <div className="flex h-full items-center justify-center gap-2 text-sm text-muted-foreground">
              <Loader2Icon className="size-4 animate-spin" />
              Starting preview…
            </div>
          ) : preview.isError || preview.data === undefined ? (
            <div className="flex h-full flex-col items-center justify-center gap-3 px-6 text-center">
              <p className="text-sm font-medium">Browser preview is unavailable</p>
              <p className="text-xs text-muted-foreground">
                The runner may be asleep, or this host may not provide an isolated preview origin.
              </p>
              <Button variant="outline" size="sm" onClick={() => void preview.refetch()}>
                <RefreshCwIcon className="size-3.5" />
                Retry
              </Button>
            </div>
          ) : (
            <ArtifactPreviewSurface
              surfaceId={artifactSurfaceId}
              title={selected.title}
              url={preview.data.url}
              visible={!selectorOpen}
            />
          )}
        </div>
      )}
    </div>
  );
}
