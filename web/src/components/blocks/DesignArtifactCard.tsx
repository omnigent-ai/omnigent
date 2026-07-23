import { ChevronRightIcon, Code2Icon, Loader2Icon } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useArtifactPreview } from "@/hooks/useArtifactPreview";
import { cn } from "@/lib/utils";
import { useArtifactViewer } from "@/shell/ArtifactViewerContext";
import { useFileViewerConversationId } from "@/shell/FileViewerContext";

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

const PREVIEW_VIEWPORT_WIDTH = 1024;
const PREVIEW_WELL_HEIGHT = 168;

interface ArtifactPreviewWellProps {
  title: string;
  url?: string;
  loading?: boolean;
}

function ArtifactPreviewWell({ title, url, loading = false }: ArtifactPreviewWellProps) {
  const wrapperRef = useRef<HTMLDivElement>(null);
  const [wellWidth, setWellWidth] = useState(560);

  useEffect(() => {
    const wrapper = wrapperRef.current;
    if (!wrapper || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width;
      if (width && width > 0) setWellWidth(width);
    });
    observer.observe(wrapper);
    return () => observer.disconnect();
  }, []);

  const scale = wellWidth / PREVIEW_VIEWPORT_WIDTH;
  const iframeHeight = Math.round(PREVIEW_WELL_HEIGHT / (scale || 1));

  return (
    <div
      data-testid="design-artifact-preview-well"
      className="mx-3 mb-3 overflow-hidden rounded-lg border border-border bg-muted/40 shadow-inner"
    >
      <div className="flex items-center gap-1.5 border-b border-border px-2.5 py-1.5">
        <span className="size-1.5 rounded-full bg-muted-foreground/40" />
        <span className="size-1.5 rounded-full bg-muted-foreground/40" />
        <span className="size-1.5 rounded-full bg-muted-foreground/40" />
        <span className="ml-1.5 h-3 flex-1 rounded border border-border bg-background/70" />
      </div>
      <div
        ref={wrapperRef}
        className="relative overflow-hidden bg-white"
        style={{ height: PREVIEW_WELL_HEIGHT }}
      >
        {url ? (
          <iframe
            title={`${title} card preview`}
            src={url}
            sandbox="allow-same-origin"
            tabIndex={-1}
            aria-hidden="true"
            loading="lazy"
            className="pointer-events-none absolute left-0 top-0 border-0 bg-white"
            style={{
              width: PREVIEW_VIEWPORT_WIDTH,
              height: iframeHeight,
              transform: `scale(${scale})`,
              transformOrigin: "top left",
            }}
          />
        ) : (
          <div className="flex h-full items-center justify-center bg-muted/20 text-muted-foreground">
            {loading ? (
              <Loader2Icon className="size-4 animate-spin" />
            ) : (
              <Code2Icon className="size-5" />
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function ConnectedArtifactPreview({
  conversationId,
  entryPath,
  title,
}: {
  conversationId: string;
  entryPath: string;
  title: string;
}) {
  const preview = useArtifactPreview(conversationId, entryPath);
  return <ArtifactPreviewWell title={title} url={preview.data?.url} loading={preview.isLoading} />;
}

export function DesignArtifactCard({ data }: DesignArtifactCardProps) {
  const openArtifact = useArtifactViewer();
  const conversationId = useFileViewerConversationId();
  const resourceLabel = `${data.resourceCount} ${data.resourceCount === 1 ? "file" : "files"}`;
  const operationLabel = data.operation === "created" ? "Created" : "Updated";
  const open = () => openArtifact?.(data.entryPath);

  return (
    <div
      role="button"
      tabIndex={0}
      data-testid="design-artifact-card"
      onClick={open}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          open();
        }
      }}
      className={cn(
        "not-prose my-1 w-full min-w-0 cursor-pointer overflow-hidden rounded-xl border border-border bg-card text-left shadow-sm transition-colors hover:bg-muted/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
      )}
      aria-label={`Open design artifact ${data.title}`}
    >
      <div className="flex items-center gap-3 px-4 pb-3 pt-3.5">
        <span className="flex size-[34px] shrink-0 items-center justify-center rounded-lg bg-muted text-foreground">
          <Code2Icon className="size-[18px]" aria-hidden="true" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-semibold text-foreground">{data.title}</span>
          <span className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
            <span className="font-mono font-medium text-foreground/80">HTML</span>
            <span aria-hidden="true" className="size-0.5 rounded-full bg-muted-foreground" />
            <span>{resourceLabel}</span>
            <span aria-hidden="true" className="size-0.5 rounded-full bg-muted-foreground" />
            <span>{operationLabel}</span>
          </span>
        </span>
        <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      </div>
      {conversationId ? (
        <ConnectedArtifactPreview
          conversationId={conversationId}
          entryPath={data.entryPath}
          title={data.title}
        />
      ) : (
        <ArtifactPreviewWell title={data.title} />
      )}
    </div>
  );
}
