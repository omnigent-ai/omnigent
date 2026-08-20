import { useEffect, useState } from "react";
import { ImageIcon } from "lucide-react";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";
import { getOmnigentHostConfig } from "@/lib/host";
import { authenticatedFetch } from "@/lib/identity";
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
 * Fixed-height box every inline preview renders into, reserved before the bytes
 * arrive and unchanged once they land. The chat scroller runs with
 * `overflow-anchor: none` (history prepends own the anchoring) and its
 * resize-compensating observer is iOS-only, so an image that grew on decode
 * would shove the transcript down under the reader with nothing to absorb it.
 */
const PREVIEW_BOX = "flex h-64 max-w-full shrink-0 items-center justify-center";

/**
 * Cap on the image itself, in the same absolute unit as the box's height so the
 * two can't drift apart. It has to be absolute rather than `max-h-full`: the
 * lightbox wraps the image in an auto-height button, and a percentage height
 * resolves against that wrapper, leaving a tall image free to overflow the box.
 */
const PREVIEW_IMAGE = "max-h-64 max-w-full";

/** Placeholder width, so the box holds a slot on the row before its image lands. */
const PREVIEW_PLACEHOLDER_BOX = "flex h-64 w-40 shrink-0 items-center justify-center";

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
    return (
      <div className={PREVIEW_BOX}>
        <ZoomableImage
          src={path}
          alt={alt}
          className={cn(PREVIEW_IMAGE, className)}
          // Offscreen history images cost nothing until scrolled to, and
          // decoding off the main thread keeps the swap from blocking paint.
          loading="lazy"
          decoding="async"
        />
      </div>
    );
  }
  return <EmbeddedSessionImage path={path} alt={alt} className={className} />;
}

type LoadState = "loading" | "loaded" | "error";

/**
 * Object URLs for image bytes already pulled through the host fetcher, keyed by
 * content path. A file id addresses one immutable set of bytes, so a hit is
 * always current — re-opening a session re-renders the same attachments, and
 * without this every remount re-downloaded and re-decoded megabytes.
 *
 * Bounded and evicted least-recently-used: entries outlive the components that
 * created them, so nothing else would ever release them.
 */
const MAX_CACHED_IMAGES = 32;
const blobUrlCache = new Map<string, string>();

/** In-flight fetches, so concurrent mounts of one path share a single download. */
const inFlight = new Map<string, Promise<string>>();

function cacheBlobUrl(path: string, url: string): void {
  blobUrlCache.set(path, url);
  while (blobUrlCache.size > MAX_CACHED_IMAGES) {
    const oldest = blobUrlCache.keys().next();
    if (oldest.done) break;
    const evicted = blobUrlCache.get(oldest.value);
    blobUrlCache.delete(oldest.value);
    if (evicted) URL.revokeObjectURL(evicted);
  }
}

function loadBlobUrl(path: string): Promise<string> {
  const cached = blobUrlCache.get(path);
  if (cached) {
    // Re-insert to mark it most-recently-used.
    blobUrlCache.delete(path);
    blobUrlCache.set(path, cached);
    return Promise.resolve(cached);
  }
  const pending = inFlight.get(path);
  if (pending) return pending;

  // Route this host-scoped file-content read through authenticatedFetch (not
  // hostFetch) so it stamps the slice-key routing header and reaches the replica
  // holding the session's runner tunnel (the oxlint no-restricted-imports rule
  // forbids direct hostFetch here for exactly this reason).
  const request = authenticatedFetch(path)
    .then((res) => (res.ok ? res.blob() : Promise.reject(new Error(`HTTP ${res.status}`))))
    .then((blob) => {
      const url = URL.createObjectURL(blob);
      cacheBlobUrl(path, url);
      return url;
    })
    .finally(() => {
      inFlight.delete(path);
    });
  inFlight.set(path, request);
  return request;
}

function EmbeddedSessionImage({ path, alt, className }: SessionImageProps) {
  // Seed from cache so a revisited image paints on the first frame instead of
  // flashing the placeholder.
  const [blobUrl, setBlobUrl] = useState<string | null>(() =>
    path ? (blobUrlCache.get(path) ?? null) : null,
  );
  const [state, setState] = useState<LoadState>(() =>
    path && blobUrlCache.has(path) ? "loaded" : "loading",
  );

  useEffect(() => {
    if (!path) {
      setState("error");
      return;
    }
    const cached = blobUrlCache.get(path);
    if (cached) {
      setBlobUrl(cached);
      setState("loaded");
      return;
    }
    setState("loading");
    setBlobUrl(null);
    let cancelled = false;
    loadBlobUrl(path)
      .then((url) => {
        if (cancelled) return;
        setBlobUrl(url);
        setState("loaded");
      })
      .catch(() => {
        if (!cancelled) setState("error");
      });
    return () => {
      cancelled = true;
    };
  }, [path]);

  if (state === "error") {
    return (
      <div
        role="img"
        aria-label={alt}
        className={cn(
          "flex items-center gap-1.5 rounded-md border border-border bg-muted px-2 py-1.5 text-sm text-muted-foreground",
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
        className={cn(
          PREVIEW_PLACEHOLDER_BOX,
          "rounded-md border border-border bg-muted text-muted-foreground",
          className,
        )}
      >
        <Spinner />
      </div>
    );
  }

  return (
    <div className={PREVIEW_BOX}>
      <ZoomableImage
        src={blobUrl}
        alt={alt}
        className={cn(PREVIEW_IMAGE, className)}
        decoding="async"
      />
    </div>
  );
}
