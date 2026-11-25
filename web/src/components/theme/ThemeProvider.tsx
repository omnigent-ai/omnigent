import { type ReactNode, useEffect, useRef } from "react";
import { ThemeProvider as NextThemesProvider, useTheme } from "next-themes";
import { usePrefersDarkColorScheme } from "@/hooks/usePrefersDarkColorScheme";
import { useHostTheme } from "@/lib/embedded";
import { setThemeSource } from "@/lib/nativeBridge";
import { useThemeModePreview } from "@/store/themeModePreview";
import { isThemeMode, type ResolvedThemeMode, type ThemeMode } from "./themeMode";

/**
 * Mirrors the shown theme mode, a preview included, onto native shell chrome.
 * Unmounting mid-preview hands the shell the saved mode again. Renders nothing.
 */
function NativeThemeSync() {
  const { theme } = useTheme();
  const previewMode = useThemeModePreview();
  const savedRef = useRef<ThemeMode | null>(null);
  const sentRef = useRef<ThemeMode | null>(null);
  savedRef.current = isThemeMode(theme) ? theme : null;
  const shown = previewMode ?? savedRef.current;
  useEffect(() => {
    if (shown === null) return;
    sentRef.current = shown;
    setThemeSource(shown);
  }, [shown]);
  useEffect(
    () => () => {
      const saved = savedRef.current;
      if (saved !== null && sentRef.current !== saved) setThemeSource(saved);
    },
    [],
  );
  return null;
}

/**
 * The concrete mode to force while a picker previews one. System resolves
 * live from the host or the OS so it keeps following them during the preview.
 */
function usePreviewedThemeMode(): ResolvedThemeMode | undefined {
  const previewMode = useThemeModePreview();
  const hostTheme = useHostTheme();
  const prefersDark = usePrefersDarkColorScheme();
  if (previewMode === null) return undefined;
  if (previewMode !== "system") return previewMode;
  return hostTheme ?? (prefersDark ? "dark" : "light");
}

/**
 * App-wide theme provider configured for Tailwind's `.dark` class variant.
 *
 * Defaults to system preference and stores explicit user selection under
 * an web-specific key so it does not collide with unrelated local apps
 * on the same host. A picker preview is shown as `forcedTheme`, never stored.
 *
 * @param children React tree that should inherit theme context.
 * @returns React provider wrapping the app.
 */
export function ThemeProvider({
  children,
  embedded = false,
}: {
  children: ReactNode;
  embedded?: boolean;
}) {
  const previewedMode = usePreviewedThemeMode();
  return (
    <NextThemesProvider
      // Embedded CSS is scoped to its portal root, never the host document.
      attribute={embedded ? [] : "class"}
      enableColorScheme={!embedded}
      defaultTheme="system"
      enableSystem
      disableTransitionOnChange
      storageKey="web-theme"
      forcedTheme={previewedMode}
    >
      <NativeThemeSync />
      {children}
    </NextThemesProvider>
  );
}
