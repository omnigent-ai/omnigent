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
}

export function ArtifactPreviewSurface({
  surfaceId,
  title,
  url,
  visible,
}: ArtifactPreviewSurfaceProps) {
  const elementRef = useRef<HTMLDivElement>(null);
  const previewStateRef = useRef({ url, visible });
  const syncRef = useRef<() => void>(() => undefined);
  const nativeSurface = hasNativeArtifactSurface();
  previewStateRef.current = { url, visible };

  useEffect(() => {
    if (!nativeSurface) return;
    const element = elementRef.current;
    if (!element) return;

    const sync = () => {
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
        bounds: {
          x: Math.round(rect.left),
          y: Math.round(rect.top),
          width: Math.round(rect.width),
          height: Math.round(rect.height),
        },
      });
    };
    syncRef.current = sync;

    const observer = new ResizeObserver(sync);
    observer.observe(element);
    window.addEventListener("resize", sync);
    window.addEventListener("scroll", sync, true);
    sync();
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", sync);
      window.removeEventListener("scroll", sync, true);
    };
  }, [nativeSurface, surfaceId]);

  useEffect(() => {
    if (nativeSurface) syncRef.current();
  }, [nativeSurface, url, visible]);

  useEffect(
    () => () => {
      if (nativeSurface) void destroyNativeArtifactSurface(surfaceId);
    },
    [nativeSurface, surfaceId],
  );

  if (!nativeSurface) {
    return (
      <iframe
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
