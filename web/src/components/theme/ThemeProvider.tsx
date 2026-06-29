import type { ReactNode } from "react";
import { ThemeProvider as NextThemesProvider } from "next-themes";

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
      {children}
    </NextThemesProvider>
  );
}
