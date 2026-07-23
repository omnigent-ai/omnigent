import { type ReactNode, useEffect } from "react";
import { ThemeProvider as NextThemesProvider, useTheme } from "next-themes";
import { reportColorScheme } from "@/lib/nativeBridge";

/**
 * Mirrors the in-app theme onto native shell chrome. Renders nothing.
 */
function NativeThemeSync() {
  const { theme, resolvedTheme } = useTheme();
  useEffect(() => {
    if (resolvedTheme === "light" || resolvedTheme === "dark") {
      const selectedTheme =
        theme === "light" || theme === "dark" || theme === "system" ? theme : resolvedTheme;
      reportColorScheme(resolvedTheme, selectedTheme);
    }
  }, [theme, resolvedTheme]);
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
