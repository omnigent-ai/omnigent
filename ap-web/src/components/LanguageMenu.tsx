import { LanguagesIcon } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { LANGUAGE_LABELS, SUPPORTED_LANGUAGES, type SupportedLanguage } from "@/i18n";

/** The language a click should switch to: the next entry, wrapping around. */
function nextLanguage(current: SupportedLanguage): SupportedLanguage {
  const index = SUPPORTED_LANGUAGES.indexOf(current);
  return SUPPORTED_LANGUAGES[(index + 1) % SUPPORTED_LANGUAGES.length];
}

/**
 * Compact sidebar control that cycles through the supported UI languages
 * on click.
 *
 * A single icon button rather than a dropdown — mirroring
 * {@link ThemeModeMenu}'s collapsed structure and placement so the two
 * preference controls sit together in the sidebar header. The tooltip and
 * aria-label announce the language the next click will apply (e.g.
 * "Switch to Français"), matching the theme button's "Switch to System"
 * phrasing.
 *
 * Switching calls ``i18n.changeLanguage``; the browser language detector
 * (configured in ``src/i18n``) persists the choice to ``localStorage``,
 * so no manual storage write is needed here. ``resolvedLanguage`` (not
 * ``language``) drives the current value so a region tag like ``en-US``
 * still resolves to the base ``en`` entry.
 *
 * @returns Language cycle button.
 */
export function LanguageMenu() {
  const { i18n } = useTranslation("common");
  const current = (i18n.resolvedLanguage ?? "en") as SupportedLanguage;
  const next = nextLanguage(current);
  const action = `Switch to ${LANGUAGE_LABELS[next]}`;

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
          onClick={() => void i18n.changeLanguage(next)}
        >
          <LanguagesIcon className="size-4" />
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom">{action}</TooltipContent>
    </Tooltip>
  );
}
