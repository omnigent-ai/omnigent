import { useEffect, useState } from "react";
import { ImageIcon } from "lucide-react";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";
import { withBasePath } from "@/lib/basePath";
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
  /** Compact square preview used by user message attachments. */
  variant?: "preview" | "userAttachment";
}

// Size the box to the image so short screenshots don't leave empty rows.
const PREVIEW_BOX = "flex max-w-full shrink-0 items-start justify-center";

// An absolute cap also constrains images inside the auto-height zoom button.
const PREVIEW_IMAGE = "max-h-64 max-w-full";

/** Placeholder width, so the box holds a slot on the row before its image lands. */
const PREVIEW_PLACEHOLDER_BOX = "flex h-64 w-40 shrink-0 items-center justify-center";

const USER_ATTACHMENT_BOX =
  "flex size-20 shrink-0 items-center justify-center overflow-hidden rounded-lg bg-background ring-1 ring-inset ring-border/60";
const USER_ATTACHMENT_IMAGE = "max-h-20 max-w-20 object-contain";
const USER_ATTACHMENT_BUTTON = "size-20 items-center justify-center";

/** Fixed-size slot for an uploading or unavailable user attachment. */
export function ImageAttachmentPlaceholder({
  label,
  pending = false,
  className,
}: {
  label: string;
  pending?: boolean;
  className?: string;
}) {
  return (
    <div
      role={pending ? "status" : "img"}
      aria-label={pending ? `Uploading image: ${label}` : label}
      className={cn(
        USER_ATTACHMENT_BOX,
        "flex-col gap-1 p-1 text-xs text-muted-foreground",
        className,
      )}
    >
      {pending ? (
        <Spinner aria-hidden="true" />
      ) : (
        <ImageIcon className="size-3.5 shrink-0" aria-hidden="true" />
      )}
      <span className="w-full truncate text-center">{label}</span>
    </div>
  );
}

/** Label a failed image without leaving a broken image control. */
function UnavailableImage({
  alt,
  className,
  variant = "preview",
}: Pick<SessionImageProps, "alt" | "className" | "variant">) {
  if (variant === "userAttachment") {
    return <ImageAttachmentPlaceholder label={alt} className={className} />;
  }

  return (
    <div
      role="img"
      aria-label={alt}
      className={cn(
        "flex items-center gap-1.5 rounded-md border border-border bg-muted px-2 py-1.5 text-sm text-muted-foreground",
        className,
      )}
    >
      <ImageIcon className="size-3.5 shrink-0" aria-hidden="true" />
      <span className="truncate">{alt}</span>
    </div>
  );
}

/**
 * Inline preview for an uploaded image stored as a session file resource.
 *
 * Standalone the file path is same-origin, so a plain `<img src>` works (native
 * streaming + HTTP caching) and {@link InlineImage} renders it. Embedded, the host proxies
 * the API behind a path prefix and cookie+CSRF auth that a browser `<img>` GET
 * can't satisfy (no way to send the prefix or the CSRF header), so we pull the
 * bytes through the host fetcher — which handles both — and render an object
 * URL, with explicit loading and error states.
 */
export function SessionImage({ path, alt, className, variant }: SessionImageProps) {
  // Host config is installed once at embed startup and never changes, so it's
  // safe to branch on it before any hooks. Hooks live in the embedded child.
  if (!getOmnigentHostConfig().fetcher) {
    // Share sizing and error handling with transcript-carried images.
    return (
      <InlineImage
        src={path ? withBasePath(path) : path}
        alt={alt}
        className={className}
        variant={variant}
      />
    );
  }
  return <EmbeddedSessionImage path={path} alt={alt} className={className} variant={variant} />;
}

/**
 * Preview for an image the browser can load straight from `src`: a `data:` URI
 * the transcript carries, or a same-origin session file path.
 *
 * Shares sizing and failure handling with same-origin session images.
 * Only corrupt data URIs collapse to a chip; fetched paths can fail transiently.
 */
export function InlineImage({
  src,
  alt,
  className,
  variant = "preview",
}: {
  src?: string;
  alt: string;
  className?: string;
  variant?: SessionImageProps["variant"];
}) {
  // Tracked by source rather than as a bare flag, so a later block swapping in a
  // different `src` on the same slot retries instead of staying stuck failed.
  const [failedSrc, setFailedSrc] = useState<string | null>(null);

  // A truncated or corrupt `data:` URI decodes to nothing; showing the chip
  // keeps it off the lightbox.
  if (src !== undefined && failedSrc === src) {
    return <UnavailableImage alt={alt} className={className} variant={variant} />;
  }

  if (variant === "userAttachment" && !src) {
    return (
      <div
        role="status"
        aria-label={`Loading image: ${alt}`}
        className={cn(USER_ATTACHMENT_BOX, "text-muted-foreground", className)}
      >
        <Spinner aria-hidden="true" />
      </div>
    );
  }

  return (
    <div className={variant === "userAttachment" ? USER_ATTACHMENT_BOX : PREVIEW_BOX}>
      <ZoomableImage
        src={src}
        alt={alt}
        className={cn(
          variant === "userAttachment" ? USER_ATTACHMENT_IMAGE : PREVIEW_IMAGE,
          className,
        )}
        buttonClassName={variant === "userAttachment" ? USER_ATTACHMENT_BUTTON : undefined}
        // Offscreen history images cost nothing until scrolled to, and decoding
        // off the main thread keeps the swap from blocking paint.
        loading="lazy"
        decoding="async"
        // An absent src never resolved, so nothing has failed yet — latching
        // here would strand the slot on a chip once the path arrives. A
        // non-data src remains available for retry on error.
        onError={() => {
          if (src !== undefined && src.startsWith("data:")) setFailedSrc(src);
        }}
      />
    </div>
  );
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

function EmbeddedSessionImage({ path, alt, className, variant = "preview" }: SessionImageProps) {
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
    return <UnavailableImage alt={alt} className={className} variant={variant} />;
  }

  if (state === "loading" || !blobUrl) {
    return (
      <div
        role="status"
        aria-label={variant === "userAttachment" ? `Loading image: ${alt}` : "Loading image"}
        className={cn(
          variant === "userAttachment"
            ? USER_ATTACHMENT_BOX
            : `${PREVIEW_PLACEHOLDER_BOX} rounded-md border border-border bg-muted`,
          "text-muted-foreground",
          className,
        )}
      >
        <Spinner aria-hidden="true" />
      </div>
    );
  }

  return (
    <div className={variant === "userAttachment" ? USER_ATTACHMENT_BOX : PREVIEW_BOX}>
      <ZoomableImage
        src={blobUrl}
        alt={alt}
        className={cn(
          variant === "userAttachment" ? USER_ATTACHMENT_IMAGE : PREVIEW_IMAGE,
          className,
        )}
        buttonClassName={variant === "userAttachment" ? USER_ATTACHMENT_BUTTON : undefined}
        decoding="async"
      />
    </div>
  );
}
