import { useTheme } from "next-themes";
import { normalizeResolvedTheme, type ResolvedThemeMode } from "./themeMode";

/**
 * The concrete light/dark palette the app is rendering, honoring a forced theme.
 *
 * The managed/embedded island drives its palette with next-themes'
 * `forcedTheme` (see `embed.tsx`), which — unlike a normal selection —
 * does NOT feed back into `resolvedTheme`; there `resolvedTheme` stays
 * unset and normalizes to light. Editor/terminal/3D themes that read
 * `resolvedTheme` alone therefore render light on a dark embed. Prefer
 * `forcedTheme` when present; standalone never sets it, so this is identical
 * to reading `resolvedTheme` there.
 *
 * @returns Concrete `"light"` or `"dark"` mode to theme non-CSS surfaces with.
 */
export function useResolvedThemeMode(): ResolvedThemeMode {
  const { resolvedTheme, forcedTheme } = useTheme();
  return normalizeResolvedTheme(forcedTheme ?? resolvedTheme);
}
