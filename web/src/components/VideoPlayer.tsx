import { useEffect, useRef, useState } from "react";
import { DownloadIcon, PlayIcon, VideoIcon } from "lucide-react";
import { Spinner } from "@/components/ui/spinner";
import { downloadWorkspaceFile, fetchWorkspaceFileBlob } from "@/hooks/useFileContent";
import { showToast } from "@/components/ui/toast";
import { cn } from "@/lib/utils";

type VideoSource = { conversationId: string; path: string } | { src: string };
export type VideoPlayerProps = VideoSource & { title: string; className?: string };

/** Reset playback and release media when a transcript slot or file tab changes source. */
export function VideoPlayer(props: VideoPlayerProps) {
  const key = "src" in props ? props.src : `${props.conversationId}:${props.path}`;
  return <VideoPlayerContent key={key} {...props} />;
}

function VideoPlayerContent(props: VideoPlayerProps) {
  const { title, className } = props;
  const remoteSrc = "src" in props ? props.src : undefined;
  const conversationId = "conversationId" in props ? props.conversationId : undefined;
  const path = "path" in props ? props.path : undefined;
  const [requested, setRequested] = useState(false);
  const [src, setSrc] = useState(remoteSrc);
  const [failed, setFailed] = useState(false);
  const videoRef = useRef<HTMLVideoElement>(null);

  // Recordings can be large. Fetch only on Play, and release bytes on close.
  useEffect(() => {
    if (!requested || !conversationId || !path) return;
    const controller = new AbortController();
    let objectUrl: string | undefined;
    void fetchWorkspaceFileBlob(conversationId, path, controller.signal)
      .then((blob) => {
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setSrc(objectUrl);
      })
      .catch(() => {
        if (!controller.signal.aborted) setFailed(true);
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [requested, conversationId, path]);

  const loading = requested && !src && !failed;
  return (
    <span
      className={cn(
        "my-2 inline-flex w-full max-w-xl flex-col overflow-hidden rounded-xl border border-border bg-card align-top text-sm text-foreground",
        className,
      )}
    >
      {src && !failed ? (
        <video
          ref={videoRef}
          src={src}
          aria-label={title}
          controls
          playsInline
          preload="metadata"
          className="aspect-video max-h-[70vh] w-full bg-black object-contain"
          onError={() => setFailed(true)}
          onLoadedData={() => {
            // Playback after an async fetch may need a second click on some WebViews.
            if (requested) void videoRef.current?.play().catch(() => {});
          }}
        />
      ) : (
        <span className="flex aspect-video w-full flex-col items-center justify-center gap-3 bg-muted p-4 text-muted-foreground">
          {failed ? (
            <>
              <VideoIcon className="size-8" />
              <span role="status">Unable to play this video. Download it to watch locally.</span>
              <button
                type="button"
                className="rounded-md border px-3 py-1.5 hover:bg-accent"
                onClick={() => {
                  setFailed(false);
                  setRequested(false);
                  setSrc(remoteSrc);
                }}
              >
                Retry
              </button>
            </>
          ) : loading ? (
            <span role="status" className="flex items-center gap-2">
              <Spinner aria-hidden="true" role="presentation" /> Loading video…
            </span>
          ) : (
            <button
              type="button"
              aria-label={`Play video: ${title}`}
              className="flex flex-col items-center gap-3 rounded-lg p-4 hover:bg-accent hover:text-foreground"
              onClick={() => setRequested(true)}
            >
              <PlayIcon className="size-10" />
              <span>Play recording</span>
            </button>
          )}
        </span>
      )}
      <span className="flex min-w-0 items-center gap-2 px-3 py-2">
        <VideoIcon className="size-4 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1 truncate" title={title}>
          {title}
        </span>
        {remoteSrc ? (
          <a
            href={remoteSrc}
            download
            target="_blank"
            rel="noopener noreferrer"
            aria-label={`Download video: ${title}`}
          >
            <DownloadIcon className="size-4" />
          </a>
        ) : (
          <button
            type="button"
            aria-label={`Download video: ${title}`}
            className="rounded p-1 hover:bg-accent"
            onClick={() => {
              if (conversationId && path) {
                void downloadWorkspaceFile(conversationId, path).catch(() =>
                  showToast("Unable to download video."),
                );
              }
            }}
          >
            <DownloadIcon className="size-4" />
          </button>
        )}
      </span>
    </span>
  );
}
