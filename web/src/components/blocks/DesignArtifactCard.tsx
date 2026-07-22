import { ChevronRightIcon, FileCode2Icon } from "lucide-react";
import { useMemo } from "react";
import { cn } from "@/lib/utils";
import { useFileViewer } from "@/shell/FileViewerContext";
import { useArtifactViewer } from "@/shell/ArtifactViewerContext";
import { TOOL_SURFACE_WIDTH_CLASS } from "./toolSurface";

export interface DesignArtifactData {
  entryPath: string;
  artifactRoot: string;
  title: string;
  operation: "created" | "updated";
  language: "html";
  resourceCount: number;
  summary?: string;
}

function normalizedArtifactEntryPath(value: unknown): string | null {
  if (typeof value !== "string" || value.length === 0 || value.includes("\\")) return null;
  const parts = value.split("/");
  if (
    parts.length < 2 ||
    parts[0] !== "artifacts" ||
    parts.some((part) => part.length === 0 || part === "." || part === "..") ||
    !parts.at(-1)?.toLowerCase().endsWith(".html")
  ) {
    return null;
  }
  const normalized = parts.join("/");
  return normalized === value ? normalized : null;
}

function normalizedArtifactRoot(entryPath: string): string {
  return entryPath.endsWith("/index.html") ? entryPath.slice(0, -"/index.html".length) : entryPath;
}

export function parseDesignArtifactResult(
  args: Record<string, unknown>,
  output: string | null,
): DesignArtifactData | null {
  const inputPath = normalizedArtifactEntryPath(args.entry_path);
  if (inputPath === null || output === null) return null;

  let raw: unknown;
  try {
    raw = JSON.parse(output);
  } catch {
    return null;
  }
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return null;

  const result = raw as Record<string, unknown>;
  const entryPath = normalizedArtifactEntryPath(result.entry_path);
  const expectedRoot = normalizedArtifactRoot(inputPath);
  if (
    result.ok !== true ||
    entryPath !== inputPath ||
    result.artifact_root !== expectedRoot ||
    (result.operation !== "created" && result.operation !== "updated") ||
    result.language !== "html" ||
    typeof result.title !== "string" ||
    result.title.trim().length === 0 ||
    !Number.isInteger(result.resource_count) ||
    (result.resource_count as number) < 1 ||
    (result.summary !== undefined && typeof result.summary !== "string")
  ) {
    return null;
  }

  return {
    entryPath,
    artifactRoot: expectedRoot,
    title: result.title.trim(),
    operation: result.operation,
    language: "html",
    resourceCount: result.resource_count as number,
    ...(typeof result.summary === "string" && result.summary.trim().length > 0
      ? { summary: result.summary.trim() }
      : {}),
  };
}

interface DesignArtifactCardProps {
  data: DesignArtifactData;
}

export function DesignArtifactCard({ data }: DesignArtifactCardProps) {
  const openArtifact = useArtifactViewer();
  const openFile = useFileViewer();
  const resourceLabel = useMemo(
    () => `${data.resourceCount} ${data.resourceCount === 1 ? "file" : "files"}`,
    [data.resourceCount],
  );

  return (
    <button
      type="button"
      data-testid="design-artifact-card"
      onClick={() =>
        openArtifact !== null ? openArtifact(data.entryPath) : openFile?.(data.entryPath)
      }
      className={cn(
        "not-prose my-1 flex items-center gap-3 rounded-lg border border-border bg-card px-3 py-3 text-left shadow-sm transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        TOOL_SURFACE_WIDTH_CLASS,
      )}
      aria-label={`Open design artifact ${data.title}`}
    >
      <span className="flex size-10 shrink-0 items-center justify-center rounded-md border border-border bg-muted text-muted-foreground">
        <FileCode2Icon className="size-5" aria-hidden="true" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-2">
          <span className="truncate text-sm font-medium text-foreground">{data.title}</span>
          <span className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
            {data.operation === "created" ? "Created" : "Updated"}
          </span>
        </span>
        <span className="mt-0.5 block truncate font-mono text-xs text-muted-foreground">
          {data.entryPath}
        </span>
        <span className="mt-1 flex items-center gap-2 text-xs text-muted-foreground">
          <span>HTML</span>
          <span aria-hidden="true">·</span>
          <span>{resourceLabel}</span>
          {data.summary !== undefined && (
            <>
              <span aria-hidden="true">·</span>
              <span className="truncate">{data.summary}</span>
            </>
          )}
        </span>
      </span>
      <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
    </button>
  );
}
