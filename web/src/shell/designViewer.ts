// Hooks shared by the deck and wireframe viewers: the branding gate (kit or
// design system, read through the session) and fullscreen.

import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { fetchFileContent } from "@/hooks/useFileContent";
import { DESIGN_SYSTEM_POINTER } from "@/lib/designSystem";
import { isOwnerLevel } from "@/lib/permissionsApi";
import { getSessionSlim } from "@/lib/sessionsApi";
import { DESIGN_KIT_DIR, type KitFile } from "./codeViewerHelpers";
import {
  NO_BRANDING,
  dsNotApplied,
  kitNotApplied,
  loadDeckBranding,
  withNotice,
  type DeckBranding,
} from "./deckBranding";

// A stalled kit or design-system read must not leave the design blank. A full
// design system loads more files, so it gets longer once it is found.
export const DESIGN_KIT_TIMEOUT_MS = 2000;
export const DESIGN_SYSTEM_TIMEOUT_MS = 10_000;
const KIT_TIMED_OUT = withNotice(kitNotApplied("design kit timed out"));
const SYSTEM_TIMED_OUT = withNotice(dsNotApplied("design system timed out"));

/** Workspace or absolute file read for the branding loader; a 404 means "no such file". */
async function readKitFile(conversationId: string, path: string): Promise<KitFile | null> {
  try {
    return await fetchFileContent(conversationId, path);
  } catch (e) {
    if (e instanceof Error && e.message.startsWith("404")) return null;
    throw e;
  }
}

async function isSessionOwner(conversationId: string): Promise<boolean> {
  return isOwnerLevel((await getSessionSlim(conversationId)).permissionLevel);
}

/**
 * This session's branding for `content`, or `null` while it loads, so the
 * design never flashes unbranded; branding loaded for another session counts
 * as not loaded. `sections: false` keeps only kit fonts and tokens.
 */
export function useDesignBranding(
  conversationId: string | undefined,
  content: string,
  sections = true,
): DeckBranding | null {
  const [loaded, setLoaded] = useState<{ id: string; branding: DeckBranding } | null>(null);
  // Design-system files are read once per session, not on every write.
  const systemReads = useRef<{ id: string; files: Map<string, Promise<KitFile | null>> }>(null);
  useEffect(() => {
    if (!conversationId) return;
    if (systemReads.current?.id !== conversationId) {
      systemReads.current = { id: conversationId, files: new Map() };
    }
    const cache = systemReads.current.files;
    const used = new Set<string>();
    let cancelled = false;
    const read = (path: string) => {
      if (path === DESIGN_SYSTEM_POINTER || path.startsWith(`${DESIGN_KIT_DIR}/`)) {
        return readKitFile(conversationId, path);
      }
      if (cancelled) return Promise.reject(new Error("design system load cancelled"));
      used.add(path);
      let file = cache.get(path);
      if (!file) {
        file = readKitFile(conversationId, path);
        file.catch(() => cache.delete(path));
        cache.set(path, file);
      }
      return file;
    };
    const finish = (b: DeckBranding) => {
      if (cancelled) return;
      cancelled = true;
      // Keep only what this design read, so the cache never outgrows one design.
      for (const path of cache.keys()) if (!used.has(path)) cache.delete(path);
      setLoaded({ id: conversationId, branding: b });
    };
    let timer = setTimeout(() => finish(KIT_TIMED_OUT), DESIGN_KIT_TIMEOUT_MS);
    void loadDeckBranding(
      content,
      {
        read,
        isOwner: () => isSessionOwner(conversationId),
        onDesignSystem: () => {
          clearTimeout(timer);
          timer = setTimeout(() => finish(SYSTEM_TIMED_OUT), DESIGN_SYSTEM_TIMEOUT_MS);
        },
      },
      { sections },
    ).then(finish);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [conversationId, content, sections]);
  if (!conversationId) return NO_BRANDING;
  return loaded?.id === conversationId ? loaded.branding : null;
}

/** Fullscreen for `ref`'s element; `supported` is false without the Fullscreen API. */
export function useFullscreen(ref: RefObject<HTMLElement | null>) {
  const [isFullscreen, setIsFullscreen] = useState(false);
  const supported = typeof document !== "undefined" && !!document.fullscreenEnabled;
  useEffect(() => {
    const onChange = () => setIsFullscreen(document.fullscreenElement === ref.current);
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, [ref]);
  const toggle = useCallback(() => {
    const op = document.fullscreenElement
      ? document.exitFullscreen()
      : ref.current?.requestFullscreen();
    op?.catch(() => {});
  }, [ref]);
  return { isFullscreen, supported, toggle };
}
