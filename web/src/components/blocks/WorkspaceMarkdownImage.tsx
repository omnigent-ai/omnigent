import { useContext, useEffect, useState } from "react";
import { ImageIcon } from "lucide-react";
import { ZoomableImage } from "@/components/ImageLightbox";
import { ConversationScopeContext } from "@/components/chat/conversationScope";
import { fetchFileContent, fileContentToBlob } from "@/hooks/useFileContent";
import { resolveChatFilePath, useWorkspaceEnvironment } from "@/hooks/useWorkspaceChangedFiles";
import { cn } from "@/lib/utils";
import {
  useFileViewer,
  useFileViewerConversationId,
  useWorkspacePaths,
} from "@/shell/FileViewerContext";

const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const RASTER_IMAGE_TYPES_BY_EXTENSION = new Map([
  ["png", "image/png"],
  ["jpg", "image/jpeg"],
  ["jpeg", "image/jpeg"],
  ["gif", "image/gif"],
  ["webp", "image/webp"],
  ["avif", "image/avif"],
  ["bmp", "image/bmp"],
]);
const RASTER_IMAGE_TYPES = new Set(RASTER_IMAGE_TYPES_BY_EXTENSION.values());

function rasterImageType(path: string): string | undefined {
  const extension = path.match(/\.([^.]+)$/)?.[1].toLowerCase() ?? "";
  return RASTER_IMAGE_TYPES_BY_EXTENSION.get(extension);
}

/** An assistant's workspace image, fetched with the displayed session's permissions. */
export function WorkspaceMarkdownImage({ path, alt }: { path: string; alt: string }) {
  const scopedId = useContext(ConversationScopeContext);
  const viewerId = useFileViewerConversationId();
  const viewerPaths = useWorkspacePaths();
  const conversationId = scopedId ?? viewerId;
  const usesViewerWorkspace = conversationId === viewerId;
  const environment = useWorkspaceEnvironment(usesViewerWorkspace ? undefined : conversationId);
  const root = usesViewerWorkspace ? viewerPaths.root : (environment.data?.root ?? null);
  const home = usesViewerWorkspace ? viewerPaths.home : (environment.data?.home ?? null);
  const resolved = root ? resolveChatFilePath(path.replace(/^(\.\/)+/, ""), root, home) : null;
  const relativePath = resolved && !resolved.path.startsWith("/") ? resolved.path : null;
  const openFile = useFileViewer();
  // The viewer opens paths in its own session, so another session's image stays inert. An image
  // outside the workspace (e.g. a worker's worktree) opens on click only; previews never read it.
  const openInViewer =
    openFile && conversationId && usesViewerWorkspace && resolved && rasterImageType(resolved.path)
      ? () => openFile(resolved.path)
      : null;
  const imageKey = `${conversationId ?? ""}\0${root ?? ""}\0${relativePath ?? ""}`;
  const [loaded, setLoaded] = useState<{
    key: string;
    url: string;
    blob: Blob;
    signal: AbortSignal;
  } | null>(null);
  const [failedKey, setFailedKey] = useState<string | null>(null);
  const filename = path.split("/").pop() || alt || "Image";

  useEffect(() => {
    if (!conversationId || !relativePath) return;
    const extensionType = rasterImageType(relativePath);
    if (!extensionType) return;
    const controller = new AbortController();
    let objectUrl: string | undefined;
    void fetchFileContent(conversationId, relativePath, controller.signal, { scope: "workspace" })
      .then((data) => {
        if (controller.signal.aborted) return;
        const contentType = data.content_type?.split(";")[0].trim().toLowerCase() ?? extensionType;
        if (
          data.truncated ||
          data.bytes > MAX_IMAGE_BYTES ||
          data.content.length > Math.ceil(MAX_IMAGE_BYTES / 3) * 4 ||
          !RASTER_IMAGE_TYPES.has(contentType)
        ) {
          return;
        }
        const blob = fileContentToBlob({ ...data, content_type: contentType });
        if (blob.size > MAX_IMAGE_BYTES) return;
        objectUrl = URL.createObjectURL(blob);
        setFailedKey(null);
        setLoaded({ key: imageKey, url: objectUrl, blob, signal: controller.signal });
      })
      .catch(() => {
        // Missing files, denied reads and offline hosts keep the filename chip.
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [conversationId, relativePath, imageKey]);

  if (loaded?.key !== imageKey || loaded.signal.aborted || failedKey === imageKey) {
    const chipClassName =
      "inline-flex max-w-full items-center gap-1.5 rounded-md border border-border bg-muted px-2 py-1.5 align-middle text-sm text-muted-foreground";
    const chipContent = (
      <>
        <ImageIcon className="size-3.5 shrink-0" aria-hidden="true" />
        <span className="truncate">{filename}</span>
      </>
    );
    if (openInViewer) {
      return (
        <button
          type="button"
          className={cn(
            chipClassName,
            "cursor-pointer transition-colors hover:bg-accent hover:text-foreground",
          )}
          title={path}
          aria-label={alt && alt !== filename ? `Open ${alt} (${filename})` : `Open ${filename}`}
          onClick={openInViewer}
        >
          {chipContent}
        </button>
      );
    }
    return (
      <span className={chipClassName} title={path} role="img" aria-label={alt || filename}>
        {chipContent}
      </span>
    );
  }

  return (
    // The margin sits outside the image so the download button hugs its corner.
    <span className="my-2 inline-flex max-w-full">
      <ZoomableImage
        src={loaded.url}
        alt={alt || filename}
        className="max-h-96 max-w-full rounded-md object-contain"
        downloadName={filename}
        downloadBlob={loaded.blob}
        loading="lazy"
        decoding="async"
        onError={() => {
          URL.revokeObjectURL(loaded.url);
          setFailedKey(imageKey);
        }}
      />
    </span>
  );
}
