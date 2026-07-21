import { type ReactNode, useEffect } from "react";
import { ThemeProvider as NextThemesProvider, useTheme } from "next-themes";
import { reportColorScheme } from "@/lib/nativeBridge";

/**
 * Mirrors the in-app theme onto native shell chrome. Renders nothing.
 */
function NativeThemeSync() {
  const { theme, resolvedTheme } = useTheme();
  useEffect(() => {
    // Android consumes the concrete resolved scheme (system-bar icon
    // contrast) and ignores "system"; Electron keeps whichever report comes
    // last, so a trailing "system" lets it track the OS natively while an
    // explicit selection leaves it forced — including system→light under a
    // light OS, where only `theme` changes and Electron must still be told.
    if (resolvedTheme === "light" || resolvedTheme === "dark") {
      reportColorScheme(resolvedTheme);
    }
    if (theme === "system") {
      reportColorScheme("system");
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
