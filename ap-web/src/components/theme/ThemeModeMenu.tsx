import { LaptopMinimalIcon, MoonIcon, SunIcon } from "lucide-react";
import { useTheme } from "next-themes";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useIsEmbedded } from "@/lib/embedded";
import { type ThemeMode, nextThemeMode, normalizeThemeMode } from "./themeMode";

const themeModeLabels: Record<ThemeMode, string> = {
  light: "Light",
  dark: "Dark",
  system: "System",
};

const themeModeIcons: Record<ThemeMode, typeof SunIcon> = {
  light: SunIcon,
  dark: MoonIcon,
  system: LaptopMinimalIcon,
};

/**
 * Compact sidebar control that cycles system → dark → light on click.
 *
 * A single icon button rather than a dropdown. The icon shows the
 * current mode — a sun for light, a moon for dark, and a laptop for
 * system — while the tooltip and aria-label announce the mode the next
 * click will apply (see {@link nextThemeMode}).
 *
 * @returns Theme cycle button.
 */
export function ThemeModeMenu() {
  // Embedded: the host owns the theme and `embed.tsx` forces light, so a theme
  // switcher would be a no-op. Hide it.
  const isEmbedded = useIsEmbedded();
  const { theme, systemTheme, setTheme } = useTheme();
  const mode = normalizeThemeMode(theme);
  const next = nextThemeMode(mode, systemTheme);
  const Icon = themeModeIcons[mode];
  const action = `Switch to ${themeModeLabels[next]}`;

  if (isEmbedded) return null;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={action}
          title={action}
          className="rounded-full"
          onClick={() => setTheme(next)}
        >
          <Icon className="size-4" />
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom">{action}</TooltipContent>
    </Tooltip>
  );
}
