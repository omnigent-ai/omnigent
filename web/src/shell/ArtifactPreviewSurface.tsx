import { useEffect, useRef } from "react";
import {
  destroyNativeArtifactSurface,
  hasNativeArtifactSurface,
  syncNativeArtifactSurface,
} from "@/lib/nativeBridge";

const ARTIFACT_PREVIEW_SANDBOX = "allow-scripts allow-same-origin";

interface ArtifactPreviewSurfaceProps {
  surfaceId: string;
  title: string;
  url: string;
  visible: boolean;
  refreshKey?: number;
  viewportWidth?: number;
}

export function ArtifactPreviewSurface({
  surfaceId,
  title,
  url,
  visible,
  refreshKey = 0,
  viewportWidth,
}: ArtifactPreviewSurfaceProps) {
  const elementRef = useRef<HTMLDivElement>(null);
  const previewStateRef = useRef({ url, visible, viewportWidth });
  const syncRef = useRef<() => void>(() => undefined);
  const nativeSurface = hasNativeArtifactSurface();
  previewStateRef.current = { url, visible, viewportWidth };

  useEffect(() => {
    if (!nativeSurface) return;
    const element = elementRef.current;
    if (!element) return;

    let cancelled = false;
    let observer: ResizeObserver | null = null;
    let animationFrame: number | null = null;
    const sync = () => {
      if (cancelled) return;
      const previewState = previewStateRef.current;
      const rect = element.getBoundingClientRect();
      const intersectsViewport =
        rect.bottom > 0 &&
        rect.right > 0 &&
        rect.top < window.innerHeight &&
        rect.left < window.innerWidth;
      void syncNativeArtifactSurface({
        id: surfaceId,
        url: previewState.url,
        visible: previewState.visible && intersectsViewport && rect.width > 0 && rect.height > 0,
        viewportWidth: previewState.viewportWidth,
        bounds: {
          x: Math.round(rect.left),
          y: Math.round(rect.top),
          width: Math.round(rect.width),
          height: Math.round(rect.height),
        },
      });
    };
    const scheduleSync = () => {
      if (cancelled || animationFrame !== null) return;
      animationFrame = window.requestAnimationFrame(() => {
        animationFrame = null;
        sync();
      });
    };
    queueMicrotask(() => {
      if (cancelled) return;
      syncRef.current = scheduleSync;
      observer = new ResizeObserver(scheduleSync);
      observer.observe(element);
      window.addEventListener("resize", scheduleSync);
      window.addEventListener("scroll", scheduleSync, true);
      sync();
    });
    return () => {
      cancelled = true;
      syncRef.current = () => undefined;
      if (animationFrame !== null) window.cancelAnimationFrame(animationFrame);
      observer?.disconnect();
      window.removeEventListener("resize", scheduleSync);
      window.removeEventListener("scroll", scheduleSync, true);
    };
  }, [nativeSurface, surfaceId]);

  useEffect(() => {
    if (nativeSurface) syncRef.current();
  }, [nativeSurface, url, visible, viewportWidth]);

  useEffect(
    () => () => {
      if (nativeSurface) void destroyNativeArtifactSurface(surfaceId);
    },
    [nativeSurface, surfaceId],
  );

  if (!nativeSurface) {
    return (
      <iframe
        key={refreshKey}
        title={`${title} preview`}
        src={url}
        sandbox={ARTIFACT_PREVIEW_SANDBOX}
        className="h-full w-full border-0 bg-white"
      />
    );
  }

  return (
    <div ref={elementRef} aria-label={`${title} preview`} className="h-full w-full bg-white" />
  );
}
