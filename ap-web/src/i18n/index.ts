/**
 * i18next bootstrap for ap-web.
 *
 * Imported once (for side effects) from ``main.tsx`` before the app
 * renders, so the configured ``i18n`` instance is ready by first paint.
 *
 * Language is resolved by ``i18next-browser-languagedetector`` in this
 * order: a previously persisted choice in ``localStorage`` (key
 * ``omnigent:language``, matching the app's other ``omnigent:*`` prefs),
 * then the browser's ``navigator`` languages. The detector also caches
 * the active language back to that key, so a user's pick survives
 * reloads without a hand-rolled storage helper. Anything outside
 * ``supportedLngs`` falls back to English.
 *
 * Scope note: only the high-value flows (auth / nav / shared chrome) are
 * translated today. Components that still hardcode English render fine —
 * a missing key falls back to the ``en`` resource, then to the key
 * itself — so the rest of the app can be migrated incrementally by
 * repeating the ``useTranslation`` + ``t("…")`` pattern.
 */

import i18n from "i18next";
import LanguageDetector from "i18next-browser-languagedetector";
import { initReactI18next } from "react-i18next";

import enCommon from "./locales/en/common.json";
import enNav from "./locales/en/nav.json";
import frCommon from "./locales/fr/common.json";
import frNav from "./locales/fr/nav.json";

/** Languages the UI ships translations for. */
export const SUPPORTED_LANGUAGES = ["en", "fr"] as const;
export type SupportedLanguage = (typeof SUPPORTED_LANGUAGES)[number];

/** Human-readable labels for the language switcher (in their own tongue). */
export const LANGUAGE_LABELS: Record<SupportedLanguage, string> = {
  en: "English",
  fr: "Français",
};

/** localStorage key the detector reads/writes — follows the app's `omnigent:*` convention. */
export const LANGUAGE_STORAGE_KEY = "omnigent:language";

export const resources = {
  en: { common: enCommon, nav: enNav },
  fr: { common: frCommon, nav: frNav },
} as const;

void i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources,
    supportedLngs: [...SUPPORTED_LANGUAGES],
    fallbackLng: "en",
    defaultNS: "common",
    ns: ["common", "nav"],
    detection: {
      order: ["localStorage", "navigator"],
      lookupLocalStorage: LANGUAGE_STORAGE_KEY,
      caches: ["localStorage"],
    },
    interpolation: {
      // React already escapes rendered values — double-escaping would
      // mangle interpolated strings.
      escapeValue: false,
    },
  });

export default i18n;
