import { useEffect, useState } from "react";
import { ImageIcon } from "lucide-react";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";
import { getOmnigentHostConfig, hostFetch } from "@/lib/host";
import { ZoomableImage } from "@/components/ImageLightbox";

export interface SessionImageProps {
  /**
   * web file-content path (`/v1/sessions/.../resources/files/.../content`),
   * or `undefined` while no session is loaded yet.
   */
  path?: string;
  alt: string;
  className?: string;
}

/**
 * Inline preview for an uploaded image stored as a session file resource.
 *
 * Standalone the file path is same-origin, so a plain `<img src>` works and we
 * keep it as-is (native streaming + HTTP caching). Embedded, the host proxies
 * the API behind a path prefix and cookie+CSRF auth that a browser `<img>` GET
 * can't satisfy (no way to send the prefix or the CSRF header), so we pull the
 * bytes through the host fetcher — which handles both — and render an object
 * URL, with explicit loading and error states.
 */
export function SessionImage({ path, alt, className }: SessionImageProps) {
  // Host config is installed once at embed startup and never changes, so it's
  // safe to branch on it before any hooks. Hooks live in the embedded child.
  if (!getOmnigentHostConfig().fetcher) {
    return <ZoomableImage src={path} alt={alt} className={className} />;
  }
  return <EmbeddedSessionImage path={path} alt={alt} className={className} />;
}

type LoadState = "loading" | "loaded" | "error";

function EmbeddedSessionImage({ path, alt, className }: SessionImageProps) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [state, setState] = useState<LoadState>("loading");

  useEffect(() => {
    if (!path) {
      setState("error");
      return;
    }
    setState("loading");
    setBlobUrl(null);
    let cancelled = false;
    let objectUrl: string | null = null;
    hostFetch(path)
      .then((res) => (res.ok ? res.blob() : Promise.reject(new Error(`HTTP ${res.status}`))))
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setBlobUrl(objectUrl);
        setState("loaded");
      })
      .catch(() => {
        if (!cancelled) setState("error");
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [path]);

  if (state === "error") {
    return (
      <div
        role="img"
        aria-label={alt}
        className={cn(
          "flex items-center gap-1.5 rounded-md border border-border bg-muted px-2 py-1.5 text-xs text-muted-foreground",
          className,
        )}
      >
        <ImageIcon className="size-3.5 shrink-0" />
        <span className="truncate">{alt}</span>
      </div>
    );
  }

  if (state === "loading" || !blobUrl) {
    return (
      <div
        role="status"
        aria-label="Loading image"
        // Square placeholder keeps the bubble from collapsing before the
        // (unknown-dimension) image resolves.
        className={cn(
          "flex size-24 items-center justify-center rounded-md border border-border bg-muted text-muted-foreground",
          className,
        )}
      >
        <Spinner />
      </div>
    );
  }

  return <ZoomableImage src={blobUrl} alt={alt} className={className} />;
}
