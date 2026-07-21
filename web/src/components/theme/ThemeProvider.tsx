import { type ReactNode, useEffect } from "react";
import { ThemeProvider as NextThemesProvider, useTheme } from "next-themes";
import { reportColorScheme } from "@/lib/nativeBridge";

/**
 * Mirrors the in-app theme selection onto the Electron shell (nativeTheme), so
 * the shell-owned update overlay, native dialogs, and menus follow the theme
 * switcher rather than only the OS. No-op outside Electron. Renders nothing.
 */
function NativeThemeSync() {
  const { theme } = useTheme();
  useEffect(() => {
    if (theme === "light" || theme === "dark" || theme === "system") {
      reportColorScheme(theme);
    }
  }, [theme]);
  return null;
}

/**
 * App-wide theme provider configured for Tailwind's `.dark` class variant.
 *
 * Defaults to system preference and stores explicit user selection under
 * an web-specific key so it does not collide with unrelated local apps
 * on the same host.
 *
 * @param children React tree that should inherit theme context.
 * @returns React provider wrapping the app.
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  return (
    <NextThemesProvider
      attribute="class"
      defaultTheme="system"
      enableSystem
      disableTransitionOnChange
      storageKey="web-theme"
    >
      <NativeThemeSync />
      {children}
    </NextThemesProvider>
  );
}
