import { type ReactNode, useEffect } from "react";
import { ThemeProvider as NextThemesProvider, useTheme } from "next-themes";
import { setThemeSource } from "@/lib/nativeBridge";

/**
 * Mirrors the in-app theme selection onto native shell chrome. Renders nothing.
 */
function NativeThemeSync() {
  const { theme } = useTheme();
  useEffect(() => {
    if (theme === "light" || theme === "dark" || theme === "system") {
      setThemeSource(theme);
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
export function ThemeProvider({
  children,
  embedded = false,
}: {
  children: ReactNode;
  embedded?: boolean;
}) {
  return (
    <NextThemesProvider
      // Embedded CSS is scoped to its portal root, never the host document.
      attribute={embedded ? [] : "class"}
      enableColorScheme={!embedded}
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
