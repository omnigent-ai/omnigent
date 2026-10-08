import { useEffect, useId, useRef, useState } from "react";
import {
  ChevronRightIcon,
  ChevronUpIcon,
  DownloadIcon,
  ListVideoIcon,
  PlayIcon,
  VideoIcon,
} from "lucide-react";
import { Spinner } from "@/components/ui/spinner";
import {
  downloadWorkspaceFile,
  fetchWorkspaceFileBlob,
  useFileContent,
} from "@/hooks/useFileContent";
import { showToast } from "@/components/ui/toast";
import { cn } from "@/lib/utils";
import { formatVideoTime } from "@/lib/video";
import { parseVideoChapters, type VideoChapter } from "@/lib/videoChapters";

type VideoSource = { conversationId: string; path: string } | { src: string };
export type VideoPlayerProps = VideoSource & {
  title: string;
  className?: string;
  onOpenFile?: () => void;
};

/** Reset playback and release media when a transcript slot or file tab changes source. */
export function VideoPlayer(props: VideoPlayerProps) {
  const key = "src" in props ? props.src : `${props.conversationId}:${props.path}`;
  return <VideoPlayerContent key={key} {...props} />;
}

function VideoPlayerContent(props: VideoPlayerProps) {
  const { title, className, onOpenFile } = props;
  const remoteSrc = "src" in props ? props.src : undefined;
  const conversationId = "conversationId" in props ? props.conversationId : undefined;
  const path = "path" in props ? props.path : undefined;
  const [requested, setRequested] = useState(false);
  const [src, setSrc] = useState(remoteSrc);
  const [failed, setFailed] = useState(false);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState<number | null>(null);
  const [wide, setWide] = useState(false);
  const [actionsOpen, setActionsOpen] = useState(true);
  const actionsId = useId();
  const showActionsRef = useRef<HTMLButtonElement>(null);
  const hideActionsRef = useRef<HTMLButtonElement>(null);
  const cardRef = useRef<HTMLSpanElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const pendingSeek = useRef<number | null>(null);
  const chapterQuery = useFileContent(conversationId, path ? `${path}.chapters.vtt` : null, {
    retry: false,
  });
  const chapterContent = chapterQuery.data;
  const [chapters, setChapters] = useState<VideoChapter[]>([]);
  useEffect(() => {
    setChapters([]);
    if (chapterContent?.encoding !== "utf-8" || chapterContent.truncated) return;
    const controller = new AbortController();
    void parseVideoChapters(chapterContent.content, controller.signal).then((parsed) => {
      if (!controller.signal.aborted) setChapters(parsed);
    });
    return () => controller.abort();
  }, [chapterContent]);
  const activeChapter = chapters.findLastIndex(
    (chapter) => chapter.time <= time && time < chapter.end,
  );
  useEffect(() => {
    const card = cardRef.current;
    if (!card) return;
    const observer = new ResizeObserver(([entry]) => setWide(entry.contentRect.width >= 640));
    observer.observe(card);
    return () => observer.disconnect();
  }, []);
  const seek = (target: number) => {
    const video = videoRef.current;
    if (video && video.readyState >= 1) {
      video.currentTime = target;
    } else {
      pendingSeek.current = target;
      setRequested(true);
    }
    setTime(target);
  };

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
      ref={cardRef}
      className={cn(
        "my-2 inline-flex max-w-full flex-col overflow-hidden rounded-xl border border-border bg-card align-top text-sm text-foreground",
        chapters.length ? "w-[64rem]" : "w-[36rem]",
        className,
      )}
    >
      <span
        className={cn(
          "grid min-w-0",
          chapters.length && actionsOpen && wide && "grid-cols-[minmax(0,1fr)_18rem]",
        )}
      >
        <span className="flex min-w-0 flex-col">
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
              onTimeUpdate={(event) => setTime(event.currentTarget.currentTime)}
              onSeeked={(event) => setTime(event.currentTarget.currentTime)}
              onDurationChange={(event) => {
                if (Number.isFinite(event.currentTarget.duration))
                  setDuration(event.currentTarget.duration);
              }}
              onLoadedMetadata={(event) => {
                const video = event.currentTarget;
                if (Number.isFinite(video.duration)) setDuration(video.duration);
                if (pendingSeek.current !== null) {
                  video.currentTime = Number.isFinite(video.duration)
                    ? Math.min(pendingSeek.current, video.duration)
                    : pendingSeek.current;
                  pendingSeek.current = null;
                }
              }}
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
                  <span role="status">
                    Unable to play this video. Download it to watch locally.
                  </span>
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
            {onOpenFile ? (
              <button
                type="button"
                className="min-w-0 flex-1 truncate text-left underline decoration-dotted underline-offset-2 hover:text-primary"
                title={title}
                onClick={onOpenFile}
              >
                {title}
              </button>
            ) : (
              <span className="min-w-0 flex-1 truncate" title={title}>
                {title}
              </span>
            )}
            {chapters.length > 0 && !actionsOpen && (
              <button
                ref={showActionsRef}
                type="button"
                aria-label="Show recording actions"
                aria-expanded={false}
                aria-controls={actionsId}
                title="Show recording actions"
                className="flex shrink-0 items-center gap-1 rounded px-2 py-1 hover:bg-accent"
                onClick={() => {
                  setActionsOpen(true);
                  requestAnimationFrame(() => hideActionsRef.current?.focus());
                }}
              >
                <ListVideoIcon aria-hidden="true" className="size-4" />
                Actions
              </button>
            )}
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
        {chapters.length > 0 && (
          <span
            id={actionsId}
            hidden={!actionsOpen}
            role="group"
            aria-label={`Recording actions: ${title}`}
            className={cn(
              "flex min-w-0 flex-col border-border",
              wide ? "relative border-l" : "border-t",
              !actionsOpen && "hidden",
            )}
          >
            <span className={cn("flex min-h-0 flex-col", wide && "absolute inset-0")}>
              <span className="flex shrink-0 flex-wrap items-center gap-3 border-b border-border px-3 py-2 font-medium">
                <span className="flex-1">Recording actions</span>
                <button
                  ref={hideActionsRef}
                  type="button"
                  aria-label="Hide recording actions"
                  aria-expanded={true}
                  aria-controls={actionsId}
                  title="Hide recording actions"
                  className="shrink-0 rounded p-1 hover:bg-accent"
                  onClick={() => {
                    setActionsOpen(false);
                    requestAnimationFrame(() => showActionsRef.current?.focus());
                  }}
                >
                  {wide ? (
                    <ChevronRightIcon aria-hidden="true" className="size-4" />
                  ) : (
                    <ChevronUpIcon aria-hidden="true" className="size-4" />
                  )}
                </button>
              </span>
              <span
                className={cn(
                  "flex min-h-0 flex-col gap-1 overflow-y-auto overscroll-contain p-2",
                  wide ? "flex-1" : "max-h-72",
                )}
              >
                {chapters.map((chapter, index) => (
                  <button
                    type="button"
                    key={`${chapter.time}:${chapter.end}:${chapter.title}`}
                    aria-current={index === activeChapter ? "step" : undefined}
                    disabled={failed || (duration !== null && chapter.time >= duration)}
                    className={cn(
                      "flex shrink-0 items-start gap-2 rounded-md p-2 text-left hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50",
                      index === activeChapter && "bg-accent text-accent-foreground",
                    )}
                    onClick={() => seek(chapter.time)}
                  >
                    <PlayIcon
                      aria-hidden="true"
                      className="mt-0.5 size-4 shrink-0 text-muted-foreground"
                    />
                    <span className="min-w-0 flex-1 break-words">{chapter.title}</span>
                    <span className="shrink-0 font-mono text-xs text-muted-foreground">
                      {formatVideoTime(chapter.time)}
                    </span>
                  </button>
                ))}
              </span>
              {chapterQuery.isFetching && (
                <span role="status" className="shrink-0 px-3 py-2 text-xs text-muted-foreground">
                  Updating actions…
                </span>
              )}
            </span>
          </span>
        )}
      </span>
    </span>
  );
}
