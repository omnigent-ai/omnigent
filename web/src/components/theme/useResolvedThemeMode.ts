import { useTheme } from "next-themes";
import { useHostTheme } from "@/lib/embedded";
import { normalizeResolvedTheme, normalizeThemeMode, type ResolvedThemeMode } from "./themeMode";

/** The active app palette, including the host preference in an automatic browser embed. */
export function useResolvedThemeMode(): ResolvedThemeMode {
  const { theme, resolvedTheme, forcedTheme } = useTheme();
  const hostTheme = useHostTheme();
  return normalizeResolvedTheme(
    forcedTheme ??
      (normalizeThemeMode(theme) === "system" ? hostTheme : undefined) ??
      resolvedTheme,
  );
}
