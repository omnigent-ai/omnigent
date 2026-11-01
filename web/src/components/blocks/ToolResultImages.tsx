// Opt-in previews under a tool row for the images its call read or returned,
// so a user can follow screenshot-driven work without expanding each call.

import { useMemo } from "react";
import { InlineImage, LoadingImage, UnavailableImage } from "@/components/SessionImage";
import { fileContentToBlob, useFileContent } from "@/hooks/useFileContent";
import { useObjectUrl } from "@/hooks/useObjectUrl";
import { resolveChatFilePath } from "@/hooks/useWorkspaceChangedFiles";
import type { ToolResultImage } from "@/lib/toolResultImages";
import { useFileViewerConversationId, useWorkspacePaths } from "@/shell/FileViewerContext";

export function ToolResultImages({ images }: { images: ToolResultImage[] }) {
  return (
    <div
      className="mt-1 ml-2 flex flex-wrap items-start gap-2 border-l pl-3 py-1"
      data-testid="tool-result-images"
    >
      {images.map((image) =>
        image.kind === "inline" ? (
          <InlineImage key={image.src} src={image.src} alt={image.alt} />
        ) : (
          <WorkspaceFileImage key={image.path} path={image.path} alt={image.alt} />
        ),
      )}
    </div>
  );
}

/**
 * An image file read through the session's filesystem API. The capped JSON
 * read is used rather than a download: it falls back to the host while the
 * runner sleeps instead of waking it, and the file viewer shares its cache.
 */
function WorkspaceFileImage({ path, alt }: { path: string; alt: string }) {
  const conversationId = useFileViewerConversationId();
  const { root, home, loading } = useWorkspacePaths();
  const resolved = resolveChatFilePath(path, root, home)?.path ?? null;
  // Absolute and `~` paths resolve only once the workspace environment loads.
  const waitingForPaths = resolved === null && loading && /^(?:\/|~(?:\/|$))/.test(path);
  // A best-effort preview shows the chip at once rather than retrying a missing file.
  const query = useFileContent(conversationId, resolved, { retry: false });
  const blob = useMemo(
    () => (query.data && !query.data.truncated ? fileContentToBlob(query.data) : null),
    [query.data],
  );
  const src = useObjectUrl(blob);

  if (src !== undefined) return <InlineImage src={src} alt={alt} />;
  // A pending query that is not fetching was never enabled: neither the
  // runner nor the host can serve the workspace right now.
  const unservable = query.isPending && query.fetchStatus === "idle";
  const unavailable =
    (resolved === null && !waitingForPaths) ||
    conversationId === undefined ||
    (resolved !== null && unservable) ||
    query.isError ||
    query.data?.truncated === true;
  if (unavailable) return <UnavailableImage alt={alt} />;
  return <LoadingImage />;
}
