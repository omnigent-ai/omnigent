import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { withBasePath } from "@/lib/basePath";
import {
  downloadWorkspaceFile,
  fetchWorkspaceFileBlob,
  usesDirectFileDownload,
  workspaceFileDownloadUrl,
} from "@/hooks/useFileContent";

export function VideoViewer({ conversationId, path }: { conversationId: string; path: string }) {
  return (
    <VideoPlayer key={`${conversationId}:${path}`} conversationId={conversationId} path={path} />
  );
}

function VideoPlayer({ conversationId, path }: { conversationId: string; path: string }) {
  const direct = usesDirectFileDownload();
  const [source, setSource] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (direct) return;
    const controller = new AbortController();
    let disposed = false;
    let objectUrl: string | undefined;
    void fetchWorkspaceFileBlob(conversationId, path, controller.signal).then(
      (blob) => {
        if (disposed) return;
        objectUrl = URL.createObjectURL(blob);
        setSource(objectUrl);
      },
      () => {
        if (!disposed) setFailed(true);
      },
    );
    return () => {
      disposed = true;
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [conversationId, path, direct]);

  return (
    <div className="flex h-full items-center justify-center bg-muted/30 p-4 text-ui">
      {failed ? (
        <div className="flex flex-col items-center gap-3 text-muted-foreground">
          <p>This video can't be played here.</p>
          <Button
            variant="outline"
            onClick={() => {
              void downloadWorkspaceFile(conversationId, path).catch(() =>
                toast.error("Download failed"),
              );
            }}
          >
            Download
          </Button>
        </div>
      ) : direct || source ? (
        <video
          controls
          playsInline
          preload="metadata"
          src={direct ? withBasePath(workspaceFileDownloadUrl(conversationId, path)) : source!}
          onError={() => setFailed(true)}
          className="max-h-full max-w-full rounded-md"
        />
      ) : (
        <p className="text-muted-foreground">Loading video…</p>
      )}
    </div>
  );
}
