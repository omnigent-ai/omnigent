import { useEffect, useState } from "react";
import { OttoEyes } from "@/components/OttoEyes";
import { OttoIcon } from "@/components/icons/OttoIcon";
import { useAppName, useLogoUrl } from "@/lib/branding";
import { getOmnigentHostConfig, hostFetch } from "@/lib/host";

const blobUrlCache = new Map<string, string>();
const inFlight = new Map<string, Promise<string>>();

function loadLogo(path: string): Promise<string> {
  const cached = blobUrlCache.get(path);
  if (cached) return Promise.resolve(cached);
  const pending = inFlight.get(path);
  if (pending) return pending;
  const request = hostFetch(path)
    .then((response) =>
      response.ok ? response.blob() : Promise.reject(new Error(`HTTP ${response.status}`)),
    )
    .then((blob) => {
      const url = URL.createObjectURL(blob);
      blobUrlCache.set(path, url);
      return url;
    })
    .finally(() => inFlight.delete(path));
  inFlight.set(path, request);
  return request;
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
}: {
  path: string;
  appName: string;
  className?: string;
  variant: "eyes" | "icon";
}) {
  const [src, setSrc] = useState(() => blobUrlCache.get(path) ?? null);
  useEffect(() => {
    let alive = true;
    void loadLogo(path)
      .then((url) => {
        if (alive) setSrc(url);
      })
      .catch(() => {
        if (alive) setSrc(null);
      });
    return () => {
      alive = false;
    };
  }, [path]);
  return src ? (
    <img src={src} className={className} alt={appName} />
  ) : (
    <FallbackLogo className={className} variant={variant} />
  );
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
      return (
        <EmbeddedBrandLogo
          path={logoUrl}
          appName={appName}
          className={className}
          variant={variant}
        />
      );
    }
    return <img src={logoUrl} className={className} alt={appName} />;
  }
  return <FallbackLogo className={className} variant={variant} />;
}
