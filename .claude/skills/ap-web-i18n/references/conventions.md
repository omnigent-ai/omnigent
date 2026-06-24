# ap-web i18n conventions

Concrete rules for this codebase. Read this when you need the exact
namespace split, key-naming style, French style, or wiring patterns.

## Architecture (as of this writing — verify against `src/i18n/index.ts`)

- Stack: `i18next` + `react-i18next` + `i18next-browser-languagedetector`.
- Languages: `en`, `fr` (`SUPPORTED_LANGUAGES`). `en` is the source of truth.
- Namespaces: `common` (default) and `nav`. Each is one JSON file per
  language under `src/i18n/locales/<lng>/<ns>.json`.
- Resources are **statically imported** in `src/i18n/index.ts`. Adding a
  *new namespace* (not just keys) means importing it there and registering
  it in both `resources` and the `ns: [...]` array. Adding keys to an
  existing namespace needs no code change — just edit the JSON.
- Fallback chain: missing key → `en` resource → the raw key string. This is
  why drift is invisible at runtime and why `check_parity.py` exists.
- `escapeValue: false` — React escapes for us. Don't pre-escape.

## Namespace: which one?

- `nav` — sidebar, session list, conversation chrome, navigation rail,
  panel toggles. Roughly: anything in `src/shell/Sidebar*`, `FolderTree`,
  `FlatFileList`, the session/conversation action menus, panel headers.
- `common` — everything else: auth pages, modals, buttons, toasts, empty
  states, form labels, errors.

When unsure, prefer `common`. Don't invent a third namespace unless the
user asks — a new namespace is a code change in `index.ts` and raises the
surface area for parity bugs.

## Key naming

- camelCase, descriptive, grouped by feature prefix when natural:
  `signIn`, `signingIn`, `passwordsMismatch`, `deleteConversationTitle`.
- Reuse before adding. A generic `cancel`, `save`, `close`, `retry`,
  `loading` almost certainly already exists in `common` — grep first.
  Duplicating "Cancel" as `cancelButton` is a smell.
- Interpolation uses `{{name}}`: `"switchToLanguage": "Switch to {{language}}"`.
- Plurals use i18next suffixes — define **both** `_one` and `_other`:
  ```json
  "inboxItemsWaiting_one": "1 inbox item waiting",
  "inboxItemsWaiting_other": "{{count}} inbox items waiting"
  ```
  Call as `t("inboxItemsWaiting", { count })`. French plural rules differ
  from English (0 and 1 are both singular in French) but i18next handles
  the rule selection — you just supply `_one` / `_other`, and it picks.

## French style

- Match the register of the existing fr files: concise UI French, sentence
  case (not Title Case), no trailing periods on button/label text, periods
  on full descriptive sentences.
- Keep interpolation placeholders **byte-identical** to English:
  `{{count}}`, `{{language}}` must appear verbatim in the French value, or
  `check_parity.py` fails and the runtime breaks.
- Preserve leading/trailing spaces and ellipses (`…`) that the English
  value uses for layout (e.g. `"Loading…"` → `"Chargement…"`).
- Don't translate proper nouns / product names (e.g. "Omnigent", "Claude",
  "GitHub") or code tokens shown in `<code>` (`whoami`).
- Examples from the existing files (keep consistent with these):
  - `Sign in` → `Se connecter`
  - `Cancel` → `Annuler`
  - `Delete {{count}} session(s)?` → `Supprimer {{count}} session(s) ?`
    (note the French space before `?`)
  - `Loading…` → `Chargement…`
  - `Page not found` → `Page introuvable`

## Wiring a component (the canonical pattern)

```tsx
import { useTranslation } from "react-i18next";

function MyThing() {
  const { t } = useTranslation("common"); // or "nav"
  return (
    <button aria-label={t("close")}>{t("save")}</button>
  );
}
```

- One `useTranslation(ns)` per component; pass the namespace explicitly so
  the call site reads unambiguously even though `common` is the default.
- For a string that needs a value: `t("switchToLanguage", { language })`.
- For rich text with embedded elements (a `<code>` or `<a>` mid-sentence),
  the existing code splits into prefix/suffix keys
  (`usernameHintPrefix` + `usernameHintSuffix`) rather than using `<Trans>`.
  Follow that local pattern unless the user wants `<Trans>` introduced.
- After wiring, the literal must be **gone** from the JSX — no English
  left behind as a default argument (`t("save", "Save")` defeats parity).

## Tests: assert via keys, not English literals

This is the part that keeps strings from silently escaping translation.
Today many tests assert the rendered English directly:

```tsx
fireEvent.click(screen.getByRole("button", { name: "Archived" })); // brittle
```

That passes only because `src/test-setup.ts` boots the real i18next
singleton and jsdom resolves to `en`. But it hard-codes the English copy
into the test, so renaming a translation or checking French silently
diverges, and it gives no signal that the string *is* translated.

Instead, resolve the expected text through the same i18n instance the app
uses, so the test asserts "this UI shows the value of key X" rather than
"this UI shows the literal 'Archived'":

```tsx
import i18n from "@/i18n";

const t = i18n.getFixedT(null, "nav");
fireEvent.click(screen.getByRole("button", { name: t("archived") }));
expect(screen.getByText(t("recent"))).toBeInTheDocument();
```

- Use `i18n.getFixedT(null, "<namespace>")` once near the top of the test
  (or per `describe`) to get a namespace-bound `t`. `null` = current
  language, which is `en` under jsdom — so the assertion text is identical
  to before, but now sourced from the key.
- For interpolated strings, pass the same values the component does:
  `t("bulkDeleteConfirm", { count: 3 })`.
- This means a key rename updates test + UI together, and a missing French
  key shows up as a parity failure rather than a green test over English
  fallback.
- Don't over-rotate: strings that are genuinely not translatable (test IDs,
  fixture data like `"conv_active"`, route paths) stay as literals. Only
  swap assertions that target **translated UI copy**.

## The loop to run

1. `python scripts/scan_hardcoded.py` → triage candidates.
2. For each real hit: pick namespace, add key to `en/<ns>.json`, add the
   French translation to `fr/<ns>.json`, wire the component with `t(...)`.
3. Update any test that asserted the old literal to use `getFixedT`.
4. `python scripts/check_parity.py` → must exit 0.
5. `npm run type-check` and `npm test` in `ap-web/` → must pass.
