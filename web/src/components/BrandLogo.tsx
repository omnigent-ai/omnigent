import { useEffect, useRef, useState } from "react";
import { OttoEyes } from "@/components/OttoEyes";
import { OttoIcon } from "@/components/icons/OttoIcon";
import { useAppName, useLogoUrl } from "@/lib/branding";
import { authenticatedFetch } from "@/lib/identity";
import { getOmnigentHostConfig, getOmnigentHostGeneration } from "@/lib/host";

interface BlobUrlEntry {
  refs: number;
  url: string | null;
  discarded: boolean;
  promise: Promise<string>;
}

interface BlobUrlHandle {
  url: string | null;
  promise: Promise<string>;
  release: () => void;
}

const blobUrlCache = new Map<string, BlobUrlEntry>();
let activeHostGeneration: number | null = null;

function revokeEntry(entry: BlobUrlEntry): void {
  if (!entry.url) return;
  URL.revokeObjectURL(entry.url);
  entry.url = null;
}

function resetCacheForHost(generation: number): void {
  if (activeHostGeneration === null) {
    activeHostGeneration = generation;
    return;
  }
  if (activeHostGeneration === generation) return;
  for (const entry of blobUrlCache.values()) {
    entry.discarded = true;
    revokeEntry(entry);
  }
  blobUrlCache.clear();
  activeHostGeneration = generation;
}

function acquireLogo(path: string, generation: number): BlobUrlHandle {
  resetCacheForHost(generation);
  const key = `${generation}:${path}`;
  let entry = blobUrlCache.get(key);
  if (!entry) {
    entry = { refs: 0, url: null, discarded: false, promise: Promise.resolve("") };
    const current = entry;
    current.promise = authenticatedFetch(path)
      .then((response) =>
        response.ok ? response.blob() : Promise.reject(new Error(`HTTP ${response.status}`)),
      )
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        current.url = url;
        if (current.discarded || current.refs === 0) {
          URL.revokeObjectURL(url);
          if (blobUrlCache.get(key) === current) blobUrlCache.delete(key);
          throw new Error("Brand logo released before load completed");
        }
        return url;
      })
      .catch((error: unknown) => {
        if (blobUrlCache.get(key) === current) blobUrlCache.delete(key);
        throw error;
      });
    blobUrlCache.set(key, current);
  }
  entry.refs += 1;
  let released = false;
  return {
    url: entry.url,
    promise: entry.promise,
    release: () => {
      if (released) return;
      released = true;
      entry.refs -= 1;
      if (entry.refs > 0) return;
      entry.discarded = true;
      revokeEntry(entry);
      if (blobUrlCache.get(key) === entry) blobUrlCache.delete(key);
    },
  };
}

function FallbackLogo({ className, variant }: { className?: string; variant: "eyes" | "icon" }) {
  return variant === "eyes" ? (
    <OttoEyes className={className} />
  ) : (
    <OttoIcon className={className} aria-hidden />
  );
}

function EmbeddedBrandLogo({
  path,
  appName,
  className,
  variant,
  generation,
}: {
  path: string;
  appName: string;
  className?: string;
  variant: "eyes" | "icon";
  generation: number;
}) {
  const [src, setSrc] = useState<string | null>(null);
  const releaseRef = useRef<(() => void) | null>(null);
  useEffect(() => {
    let alive = true;
    setSrc(null);
    const handle = acquireLogo(path, generation);
    releaseRef.current = handle.release;
    if (handle.url) setSrc(handle.url);
    void handle.promise
      .then((url) => {
        if (alive) setSrc(url);
      })
      .catch(() => {
        if (alive) setSrc(null);
      });
    return () => {
      alive = false;
      releaseRef.current?.();
      releaseRef.current = null;
    };
  }, [generation, path]);
  const handleError = () => {
    releaseRef.current?.();
    releaseRef.current = null;
    setSrc(null);
  };
  return src ? (
    <img src={src} className={className} alt={appName} onError={handleError} />
  ) : (
    <FallbackLogo className={className} variant={variant} />
  );
}

function StandaloneBrandLogo({
  path,
  appName,
  className,
  variant,
}: {
  path: string;
  appName: string;
  className?: string;
  variant: "eyes" | "icon";
}) {
  const [failed, setFailed] = useState(false);
  if (failed) return <FallbackLogo className={className} variant={variant} />;
  return <img src={path} className={className} alt={appName} onError={() => setFailed(true)} />;
}

/**
 * The app's brand logo: the operator's custom logo when configured, else the
 * Otto mascot. `variant` picks the logo variant and matching fallback —
 * `"eyes"` (hero) → `main`/`OttoEyes`, `"icon"` (indicators) → `loading`/`OttoIcon`.
 */
export function BrandLogo({
  className,
  variant = "eyes",
}: {
  className?: string;
  variant?: "eyes" | "icon";
}) {
  const logoUrl = useLogoUrl(variant === "icon" ? "loading" : "main");
  const appName = useAppName();
  if (logoUrl) {
    if (getOmnigentHostConfig().fetcher) {
      const generation = getOmnigentHostGeneration();
      return (
        <EmbeddedBrandLogo
          key={`${generation}:${logoUrl}`}
          path={logoUrl}
          appName={appName}
          className={className}
          variant={variant}
          generation={generation}
        />
      );
    }
    return (
      <StandaloneBrandLogo
        key={logoUrl}
        path={logoUrl}
        appName={appName}
        className={className}
        variant={variant}
      />
    );
  }
  return <FallbackLogo className={className} variant={variant} />;
}
