/**
 * Settings page (``/settings``).
 *
 * Renders into the AppShell chat outlet (see App.tsx) so the conversations
 * sidebar stays put when you enter settings — only the main area swaps to
 * this view. Inside, a section nav (left) drives a content panel (right),
 * modeled on a desktop-app settings window; a "← Back to Omnigent" link
 * returns to the composer.
 *
 * Sections:
 *
 * - **Appearance** — theme mode (System / Light / Dark). This is the new
 *   home of the theme control that used to sit in the sidebar header.
 * - **Keyboard shortcuts** — the full shortcuts reference, shown inline.
 * - **Account** — only when the accounts auth provider is active. Absorbs
 *   the old sidebar AccountMenu: signed-in identity, change password, and
 *   sign out.
 * - **Members** / **Policies** — admin-only, accounts deploys. Server-wide
 *   management surfaces rendered as settings sub-categories (previously
 *   standalone `/members` and `/policies` pages linked from Account) so
 *   entering them stays inside settings — the sidebar keeps the section nav
 *   instead of snapping back to the conversation list.
 * - **Archived sessions** — archived sessions, moved out of the sidebar
 *   list. Not clickable; each row reveals Delete / Unarchive on hover.
 */

import { lazy, type ReactNode, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import {
  ArchiveRestoreIcon,
  KeyRoundIcon,
  LogOutIcon,
  Trash2Icon,
  UserCogIcon,
} from "lucide-react";
import {
  LaptopMinimalIcon,
  MinusIcon,
  MonitorIcon,
  MoonIcon,
  PlusIcon,
  SunIcon,
} from "lucide-react";
import { useTheme } from "next-themes";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { KeyboardShortcutsList } from "@/components/KeyboardShortcutsDialog";
import { changePassword, logout } from "@/lib/accountsApi";
import { getCurrentIsAdmin, resolveIdentity } from "@/lib/identity";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  type Conversation,
  useArchiveConversation,
  useConversations,
  useStopAndDeleteConversation,
} from "@/hooks/useConversations";
import { conversationDisplayLabel } from "@/shell/sidebarNav";
import { absoluteTime } from "@/lib/relativeTime";
import { useSettingsRoute } from "@/shell/settingsNav";
import { type ThemeMode, normalizeThemeMode } from "@/components/theme/themeMode";
import {
  applyUiFontFamily,
  applyUiFontScale,
  clampUiFontSizePx,
  readUiFontFamily,
  readUiFontSizePx,
  UI_FONT_FAMILY_DEFAULT,
  UI_FONT_SIZE_MAX,
  UI_FONT_SIZE_MIN,
  UI_FONT_SIZE_STEP,
  writeUiFontFamily,
  writeUiFontSizePx,
} from "@/lib/uiFontPreferences";
import {
  clampCodeFontSizePx,
  CODE_FONT_FAMILY_DEFAULT,
  CODE_FONT_SIZE_MAX,
  CODE_FONT_SIZE_MIN,
  CODE_FONT_SIZE_STEP,
  readCodeFontFamily,
  readCodeFontSizePx,
  writeCodeFontFamily,
  writeCodeFontSizePx,
} from "@/lib/codeFontPreferences";
import {
  readTerminalThemeMode,
  writeTerminalThemeMode,
  type TerminalThemeMode,
} from "@/lib/terminalThemePreferences";
import { useIsEmbedded } from "@/lib/embedded";
import { type CliStatus, getCliStatus, isElectronShell, resetCliPath } from "@/lib/nativeBridge";
import { cn } from "@/lib/utils";

// Admin-only management surfaces, rendered as the Members / Policies settings
// sub-categories. Visible to admins in all modes (accounts, OIDC, single-user).
// Lazy-loaded to keep the settings chunk small.
const MembersPage = lazy(() =>
  import("@/pages/MembersPage").then((m) => ({ default: m.MembersPage })),
);
const PoliciesPage = lazy(() =>
  import("@/pages/PoliciesPage").then((m) => ({ default: m.PoliciesPage })),
);

/**
 * Settings content panel. The section nav lives in the sidebar card
 * (SettingsSidebarBody); this renders only the selected section into the
 * AppShell main outlet. The active section is read from the URL so the two
 * stay in sync. PageScroll handles clearing the shell's absolute header and
 * the iOS native bars, matching the Inbox / Members pages.
 */
export function SettingsPage() {
  const info = useServerInfo();
  // A login session exists (accounts OR OIDC) when the server advertises a
  // login_url; gates the Account section so SSO users get it too.
  const hasAuthSession = info !== "loading" && info.login_url !== null;
  const { section } = useSettingsRoute();

  // Members / Policies are admin-only management surfaces that own their full
  // layout (their own PageScroll + admin gating), so they render directly —
  // NOT inside the shared section PageScroll below, which would nest two
  // scroll containers. Both self-gate to admins server-side and client-side.
  // Rendered in ANY multi-user mode (accounts AND OIDC), not gated on
  // `accountsEnabled` — the nav + pages handle admin gating, and Members runs
  // read-only under OIDC (no password actions).
  if (section === "members" || section === "policies") {
    return (
      <Suspense fallback={null}>
        {section === "members" ? <MembersPage /> : <PoliciesPage />}
      </Suspense>
    );
  }

  return (
    <PageScroll contentClassName="px-8" extraBottom="2.5rem">
      {section === "appearance" && <AppearanceSection />}
      {section === "shortcuts" && <ShortcutsSection />}
      {section === "account" && hasAuthSession && <AccountSection />}
      {section === "archived" && <ArchivedSection />}
      {section === "cli" && isElectronShell() && <LocalCliSection />}
    </PageScroll>
  );
}

/** Shared section shell: a title + optional description above the body. */
function Section({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <section>
      <h1 className="text-2xl font-semibold">{title}</h1>
      {description && <p className="mt-1 text-sm text-muted-foreground">{description}</p>}
      <div className="mt-6">{children}</div>
    </section>
  );
}

const themeCards: { mode: ThemeMode; label: string; icon: typeof SunIcon }[] = [
  { mode: "system", label: "System", icon: LaptopMinimalIcon },
  { mode: "light", label: "Light", icon: SunIcon },
  { mode: "dark", label: "Dark", icon: MoonIcon },
];

const terminalThemeCards: { mode: TerminalThemeMode; label: string; icon: typeof SunIcon }[] = [
  { mode: "auto", label: "Match app", icon: MonitorIcon },
  { mode: "light", label: "Light", icon: SunIcon },
  { mode: "dark", label: "Dark", icon: MoonIcon },
];

function TerminalThemeControl() {
  const [mode, setMode] = useState(() => readTerminalThemeMode());

  return (
    <div className="flex flex-col gap-3">
      <span className="text-sm font-medium">Terminal theme</span>
      <span className="text-sm text-muted-foreground">
        Use a light or dark terminal, or match the app.
      </span>
      <div className="grid grid-cols-3 gap-3" role="radiogroup" aria-label="Terminal theme">
        {terminalThemeCards.map(({ mode: cardMode, label, icon: Icon }) => {
          const selected = mode === cardMode;
          return (
            <button
              key={cardMode}
              type="button"
              role="radio"
              aria-checked={selected}
              data-testid={`terminal-theme-${cardMode}`}
              onClick={() => {
                setMode(cardMode);
                writeTerminalThemeMode(cardMode);
              }}
              className={cn(
                "flex flex-col items-center gap-2 rounded-lg border-2 p-4 transition-colors hover:bg-muted",
                selected ? "border-primary bg-primary/5" : "border-border",
              )}
            >
              <Icon className="size-6 text-muted-foreground" />
              <span className="text-sm font-medium">{label}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function AppearanceSection() {
  // Embedded: the host owns the theme (embed.tsx forces light), so the
  // selector would be a no-op — match ThemeModeMenu and hide it.
  const isEmbedded = useIsEmbedded();
  const { theme, setTheme } = useTheme();
  const mode = normalizeThemeMode(theme);

  return (
    <Section title="Appearance" description="Choose how Omnigent looks on this device.">
      <div className="flex flex-col gap-8">
        <div className="flex flex-col gap-3">
          <span className="text-sm font-medium">Theme</span>
          {/* Embedded: the host owns the theme (embed.tsx forces light), so the
              selector would be a no-op — match ThemeModeMenu and hide it. */}
          {isEmbedded ? (
            <p className="text-sm text-muted-foreground">
              Theme is controlled by the host application.
            </p>
          ) : (
            <div className="grid grid-cols-3 gap-3" role="radiogroup" aria-label="Theme">
              {themeCards.map(({ mode: cardMode, label, icon: Icon }) => {
                const selected = mode === cardMode;
                return (
                  <button
                    key={cardMode}
                    type="button"
                    role="radio"
                    aria-checked={selected}
                    data-testid={`theme-${cardMode}`}
                    onClick={() => setTheme(cardMode)}
                    className={cn(
                      "flex flex-col items-center gap-2 rounded-lg border-2 p-4 transition-colors hover:bg-muted",
                      selected ? "border-primary bg-primary/5" : "border-border",
                    )}
                  >
                    <Icon className="size-6 text-muted-foreground" />
                    <span className="text-sm font-medium">{label}</span>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        <TerminalThemeControl />

        <UiFontSizeControl />

        <UiFontFamilyControl />

        {/* Code font (Monaco + xterm) sits as its own two rows — labelled in full
            ("Code font size" / "Code font family") rather than under a shared
            heading — so each control reads unambiguously next to the UI-font rows
            above and it's clear these don't scale the surrounding chrome. */}
        <UiCodeFontSizeControl />

        <UiCodeFontFamilyControl />
      </div>
    </Section>
  );
}

/**
 * UI font size stepper. Scales the whole rem-based UI via the --ui-font-scale
 * variable (see lib/uiFontPreferences.ts). Applied live and persisted on every
 * change; unlike the theme picker it stays visible when embedded, since it's a
 * per-device readability pref that doesn't conflict with host theming.
 */
function UiFontSizeControl() {
  // `px` is the committed value: clamped, persisted, and applied to the UI.
  // `draft` is the raw text in the box, kept separate so mid-edit states the
  // committed value can't hold — a transient out-of-range number (e.g. "1" on
  // the way to "18") or an empty field while retyping — don't get clamped on
  // every keystroke. We only commit while typing when the draft is already a
  // valid in-range size; blur/Enter clamps and re-syncs the text.
  const [px, setPx] = useState(() => readUiFontSizePx());
  const [draft, setDraft] = useState(() => String(px));

  const commit = useCallback((next: number) => {
    const clamped = clampUiFontSizePx(next);
    setPx(clamped);
    setDraft(String(clamped));
    writeUiFontSizePx(clamped);
    applyUiFontScale(clamped);
  }, []);

  const onDraftChange = useCallback((text: string) => {
    setDraft(text);
    // Apply live only once the field holds a valid, in-range whole number;
    // leave partial/out-of-range/empty drafts untouched until blur.
    if (/^\d+$/.test(text)) {
      const value = Number(text);
      if (value >= UI_FONT_SIZE_MIN && value <= UI_FONT_SIZE_MAX) {
        setPx(value);
        writeUiFontSizePx(value);
        applyUiFontScale(value);
      }
    }
  }, []);

  // Clamp and re-sync the text to the committed value. An empty or invalid
  // draft reverts to the last committed size rather than a bogus one.
  const commitDraft = useCallback(() => {
    const value = Number(draft);
    commit(Number.isFinite(value) && draft.trim() !== "" ? value : px);
  }, [commit, draft, px]);

  const atMin = px <= UI_FONT_SIZE_MIN;
  const atMax = px >= UI_FONT_SIZE_MAX;

  return (
    <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
      <div className="flex flex-col">
        <span className="text-sm font-medium">Font size</span>
        <span className="text-sm text-muted-foreground">
          Scale the interface text and spacing on this device.
        </span>
      </div>
      {/* One cohesive pill: [ −  | value px |  + ]. Segments share the pill
          border via inner dividers rather than floating as separate boxes. */}
      <div
        role="group"
        aria-label="Font size"
        className={cn(
          "inline-flex h-9 items-stretch overflow-hidden rounded-lg border border-input bg-background transition-colors dark:bg-input/30",
          "focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50",
        )}
      >
        <StepperButton
          label="Decrease font size"
          testId="ui-font-size-dec"
          disabled={atMin}
          onClick={() => commit(px - UI_FONT_SIZE_STEP)}
        >
          <MinusIcon className="size-4" />
        </StepperButton>
        <div className="flex items-center border-x border-input px-2 tabular-nums">
          <input
            type="number"
            inputMode="numeric"
            min={UI_FONT_SIZE_MIN}
            max={UI_FONT_SIZE_MAX}
            step={UI_FONT_SIZE_STEP}
            aria-label="Font size in pixels"
            data-testid="ui-font-size-input"
            className="w-8 bg-transparent text-center text-sm font-medium tabular-nums outline-none [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
            value={draft}
            onChange={(e) => onDraftChange(e.target.value)}
            onBlur={commitDraft}
            onKeyDown={(e) => {
              if (e.key === "Enter") e.currentTarget.blur();
            }}
          />
        </div>
        <StepperButton
          label="Increase font size"
          testId="ui-font-size-inc"
          disabled={atMax}
          onClick={() => commit(px + UI_FONT_SIZE_STEP)}
        >
          <PlusIcon className="size-4" />
        </StepperButton>
      </div>
    </div>
  );
}

/**
 * UI font family picker. Free-text (Cursor-style): type any font installed on
 * this device; blank means "System default", which falls back to the existing
 * --font-sans stack. Applies live and persists on every change via the
 * --ui-font-family variable (see lib/uiFontPreferences.ts). Like the size
 * control it stays visible when embedded — a per-device readability pref that
 * doesn't conflict with host theming.
 */
function UiFontFamilyControl() {
  const [family, setFamily] = useState(() => readUiFontFamily());

  const update = useCallback((next: string) => {
    setFamily(next);
    writeUiFontFamily(next);
    applyUiFontFamily(next);
  }, []);

  const isDefault = family.trim() === UI_FONT_FAMILY_DEFAULT;

  return (
    <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
      {/* Take the remaining width (and let the longer description wrap within
          this column) so the input stays inline instead of dropping to its own
          row — matches the font-size row's alignment. */}
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="text-sm font-medium">Font family</span>
        <span className="text-sm text-muted-foreground">
          Use any font installed on this device. Leave blank for the system default.
        </span>
      </div>
      {/* Reset sits left of the input so the input is the rightmost element and
          its right edge lines up flush with the font-size stepper above.
          `invisible` (not removed) at the default keeps the row from shifting. */}
      <div role="group" aria-label="Font family" className="flex shrink-0 items-center gap-2">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          data-testid="ui-font-family-reset"
          disabled={isDefault}
          className={cn("h-9", isDefault && "invisible")}
          onClick={() => update(UI_FONT_FAMILY_DEFAULT)}
        >
          Reset
        </Button>
        <Input
          type="text"
          aria-label="UI font family"
          data-testid="ui-font-family-input"
          placeholder="System default"
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          className="h-9 w-56"
          value={family}
          onChange={(e) => update(e.target.value)}
        />
      </div>
    </div>
  );
}

/**
 * Code font size stepper. Sizes the code editor (Monaco) and terminal (xterm)
 * — fixed-pixel widgets that can't ride the chrome's --ui-font-scale variable,
 * so writing the pref emits to already-mounted editors/terminals (see
 * lib/codeFontPreferences.ts). Same free-editing draft/commit + blur-clamp
 * behavior as UiFontSizeControl; only the bounds and storage differ.
 */
function UiCodeFontSizeControl() {
  // `px` is the committed value; `draft` is the raw text in the box, kept
  // separate so a transient out-of-range/empty mid-edit state isn't clamped or
  // persisted on every keystroke. We only commit while typing when the draft is
  // already a valid in-range size; blur/Enter clamps and re-syncs the text.
  const [px, setPx] = useState(() => readCodeFontSizePx());
  const [draft, setDraft] = useState(() => String(px));

  const commit = useCallback((next: number) => {
    const clamped = clampCodeFontSizePx(next);
    setPx(clamped);
    setDraft(String(clamped));
    writeCodeFontSizePx(clamped);
  }, []);

  const onDraftChange = useCallback((text: string) => {
    setDraft(text);
    // Apply live only once the field holds a valid, in-range whole number;
    // leave partial/out-of-range/empty drafts untouched until blur.
    if (/^\d+$/.test(text)) {
      const value = Number(text);
      if (value >= CODE_FONT_SIZE_MIN && value <= CODE_FONT_SIZE_MAX) {
        setPx(value);
        writeCodeFontSizePx(value);
      }
    }
  }, []);

  // Clamp and re-sync the text to the committed value. An empty or invalid
  // draft reverts to the last committed size rather than a bogus one.
  const commitDraft = useCallback(() => {
    const value = Number(draft);
    commit(Number.isFinite(value) && draft.trim() !== "" ? value : px);
  }, [commit, draft, px]);

  const atMin = px <= CODE_FONT_SIZE_MIN;
  const atMax = px >= CODE_FONT_SIZE_MAX;

  return (
    <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
      <div className="flex flex-col">
        <span className="text-sm font-medium">Code font size</span>
        <span className="text-sm text-muted-foreground">
          Size of code in the editor and terminal.
        </span>
      </div>
      {/* One cohesive pill: [ −  | value px |  + ] — same shell as the UI
          font-size control. */}
      <div
        role="group"
        aria-label="Code font size"
        className={cn(
          "inline-flex h-9 items-stretch overflow-hidden rounded-lg border border-input bg-background transition-colors dark:bg-input/30",
          "focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50",
        )}
      >
        <StepperButton
          label="Decrease code font size"
          testId="code-font-size-dec"
          disabled={atMin}
          onClick={() => commit(px - CODE_FONT_SIZE_STEP)}
        >
          <MinusIcon className="size-4" />
        </StepperButton>
        <div className="flex items-center border-x border-input px-2 tabular-nums">
          <input
            type="number"
            inputMode="numeric"
            min={CODE_FONT_SIZE_MIN}
            max={CODE_FONT_SIZE_MAX}
            step={CODE_FONT_SIZE_STEP}
            aria-label="Code font size in pixels"
            data-testid="code-font-size-input"
            className="w-8 bg-transparent text-center text-sm font-medium tabular-nums outline-none [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
            value={draft}
            onChange={(e) => onDraftChange(e.target.value)}
            onBlur={commitDraft}
            onKeyDown={(e) => {
              if (e.key === "Enter") e.currentTarget.blur();
            }}
          />
        </div>
        <StepperButton
          label="Increase code font size"
          testId="code-font-size-inc"
          disabled={atMax}
          onClick={() => commit(px + CODE_FONT_SIZE_STEP)}
        >
          <PlusIcon className="size-4" />
        </StepperButton>
      </div>
    </div>
  );
}

/**
 * Code font family picker. Free-text (Cursor-style): type any monospace font
 * installed on this device; blank means the editor/terminal default (the shared
 * mono stack). Applies live and persists on every change via the code-font
 * pub/sub (see lib/codeFontPreferences.ts). Mirrors UiFontFamilyControl.
 */
function UiCodeFontFamilyControl() {
  const [family, setFamily] = useState(() => readCodeFontFamily());

  const update = useCallback((next: string) => {
    setFamily(next);
    writeCodeFontFamily(next);
  }, []);

  const isDefault = family.trim() === CODE_FONT_FAMILY_DEFAULT;

  return (
    <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="text-sm font-medium">Code font family</span>
        <span className="text-sm text-muted-foreground">
          Font for the code editor and terminal. Leave blank for the default.
        </span>
      </div>
      {/* Reset sits left of the input so the input's right edge lines up flush
          with the size stepper above. `invisible` (not removed) at the default
          keeps the row from shifting. */}
      <div role="group" aria-label="Code font family" className="flex shrink-0 items-center gap-2">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          data-testid="code-font-family-reset"
          disabled={isDefault}
          className={cn("h-9", isDefault && "invisible")}
          onClick={() => update(CODE_FONT_FAMILY_DEFAULT)}
        >
          Reset
        </Button>
        <Input
          type="text"
          aria-label="Code font family"
          data-testid="code-font-family-input"
          placeholder="Editor default"
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          className="h-9 w-56"
          value={family}
          onChange={(e) => update(e.target.value)}
        />
      </div>
    </div>
  );
}

/** Flanking +/- segment of the font-size pill: square, ghost-hover, no border. */
function StepperButton({
  label,
  testId,
  disabled,
  onClick,
  children,
}: {
  label: string;
  testId: string;
  disabled: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      data-testid={testId}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "flex w-9 items-center justify-center text-muted-foreground transition-colors",
        "hover:bg-muted hover:text-foreground dark:hover:bg-muted/50",
        "disabled:pointer-events-none disabled:opacity-40",
      )}
    >
      {children}
    </button>
  );
}

function ShortcutsSection() {
  return (
    <Section title="Keyboard shortcuts" description="Speed up common actions with the keyboard.">
      <KeyboardShortcutsList />
    </Section>
  );
}

/**
 * Desktop-only: shows which Omnigent CLI binary the shell resolved
 * (auto-detected or a custom override). Read-only — setting a custom path is
 * done on the connect/setup screen (the trusted surface that allows free-text
 * entry); the SPA exposes no path setter. A safe "reset to auto-detected" stays
 * here since it chooses no path.
 */
function LocalCliSection() {
  const [status, setStatus] = useState<CliStatus | null | "loading">("loading");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void getCliStatus().then(setStatus);
  }, []);

  const onReset = useCallback(async () => {
    setBusy(true);
    const next = await resetCliPath();
    setBusy(false);
    if (next) setStatus(next); // null only when the bridge is missing (old shell)
  }, []);

  if (status === "loading") {
    return (
      <Section title="Local CLI">
        <p className="text-sm text-muted-foreground">Checking…</p>
      </Section>
    );
  }

  return (
    <Section
      title="Local CLI"
      description="The Omnigent command-line tool this app uses to run a local server and connect this machine as a runner."
    >
      {status === null ? (
        <p className="text-sm text-muted-foreground">CLI status is unavailable.</p>
      ) : (
        <div className="flex flex-col gap-4">
          <div className="flex items-center gap-2 text-sm">
            <span
              aria-hidden
              className={cn(
                "size-2 rounded-full",
                status.installed ? "bg-success" : "bg-muted-foreground/40",
              )}
            />
            <span>
              {status.installed
                ? `Found${status.version ? ` · ${status.version}` : ""}`
                : "Not found"}
            </span>
          </div>

          {status.path ? (
            <div className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">
                {status.source === "configured" ? "Path (custom)" : "Path (auto-detected)"}
              </span>
              <code className="block overflow-x-auto rounded-md border border-border bg-muted/40 px-3 py-2 text-xs">
                {status.path}
              </code>
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              <p className="text-sm text-muted-foreground">
                The Omnigent CLI wasn't found. Install it, then set its path from the connect
                screen:
              </p>
              {status.installCommand && (
                <code className="block overflow-x-auto rounded-md border border-border bg-muted/40 px-3 py-2 text-xs">
                  {status.installCommand}
                </code>
              )}
            </div>
          )}

          <p className="text-xs text-muted-foreground">
            For security, a custom path can only be set from the connect screen — this prevents a
            connected server from pointing the app at a different binary. Open it from the Server
            menu (Change Server…) and use the settings gear.
          </p>

          {status.source === "configured" && (
            <div>
              <Button variant="ghost" size="sm" disabled={busy} onClick={() => void onReset()}>
                Reset to auto-detected
              </Button>
            </div>
          )}
        </div>
      )}
    </Section>
  );
}

function AccountSection() {
  const info = useServerInfo();
  const accountsEnabled = info !== "loading" && info.accounts_enabled;
  // Identity for display. Sourced from the mode-agnostic `/v1/me` probe so it
  // works under OIDC too (the accounts-only `/auth/me` doesn't exist there).
  const [me, setMe] = useState<{ id: string; is_admin: boolean } | null | "unknown">("unknown");

  // Change-password dialog state (lifted verbatim from the old AccountMenu).
  // Only used in accounts mode — OIDC identities have no local password.
  const [pwOpen, setPwOpen] = useState(false);
  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [pwBusy, setPwBusy] = useState(false);
  const [pwError, setPwError] = useState<string | null>(null);
  const [pwDone, setPwDone] = useState(false);

  useEffect(() => {
    void (async () => {
      const userId = await resolveIdentity();
      setMe(userId === null ? null : { id: userId, is_admin: getCurrentIsAdmin() });
    })();
  }, []);

  const onSignOut = useCallback(async () => {
    if (accountsEnabled) {
      // Accounts: clear the cookie via the JSON logout endpoint, then land on
      // the SPA login form.
      await logout();
      // Hard navigation so the chat store / react-query cache reset.
      window.location.href = "/login";
      return;
    }
    // OIDC: logout is a server-side GET redirect at /auth/logout that clears
    // the session cookie (and honors the IdP end-session endpoint when
    // configured). A hard navigation lets the browser follow it and resets
    // client caches.
    window.location.href = "/auth/logout";
  }, [accountsEnabled]);

  const resetPwForm = useCallback(() => {
    setOldPw("");
    setNewPw("");
    setConfirmPw("");
    setPwError(null);
    setPwDone(false);
    setPwBusy(false);
  }, []);

  const onSubmitPassword = useCallback(async () => {
    if (newPw !== confirmPw) {
      setPwError("New passwords don't match.");
      return;
    }
    setPwBusy(true);
    setPwError(null);
    const result = await changePassword({ old_password: oldPw, new_password: newPw });
    setPwBusy(false);
    if (result.ok) {
      setPwDone(true);
      setOldPw("");
      setNewPw("");
      setConfirmPw("");
    } else {
      setPwError(result.error);
    }
  }, [oldPw, newPw, confirmPw]);

  if (me === "unknown" || me === null) {
    return <Section title="Account">{null}</Section>;
  }

  return (
    <Section title="Account">
      <div className="flex flex-col gap-6">
        <div className="flex items-center gap-3">
          <span className="flex size-10 shrink-0 items-center justify-center rounded-md border border-border">
            <UserCogIcon className="size-5" />
          </span>
          <div className="min-w-0">
            <div className="truncate font-medium">
              {me.id}
              {me.is_admin && (
                <span className="ml-1 text-xs font-normal text-muted-foreground">(admin)</span>
              )}
            </div>
          </div>
        </div>

        {/* Members / Policies used to live here as links to standalone pages.
            They're now first-class settings sub-categories in the sidebar nav
            (Admin group), so entering them keeps the settings surface put
            instead of navigating away from /settings. */}

        <div className="flex flex-col gap-1">
          {/* Change password is accounts-only — an OIDC identity's password
              lives with the IdP, so there's nothing to change here. */}
          {accountsEnabled && (
            <Button
              variant="ghost"
              className="w-full justify-start gap-2"
              onClick={() => {
                resetPwForm();
                setPwOpen(true);
              }}
            >
              <KeyRoundIcon className="size-4" /> Change password
            </Button>
          )}
          <Button
            variant="ghost"
            className="w-full justify-start gap-2"
            onClick={() => void onSignOut()}
          >
            <LogOutIcon className="size-4" /> Sign out
          </Button>
        </div>
      </div>

      <Dialog
        open={pwOpen}
        onOpenChange={(open) => {
          setPwOpen(open);
          if (!open) resetPwForm();
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Change password</DialogTitle>
            <DialogDescription>
              {pwDone
                ? "Your password has been changed."
                : "Enter your current password and choose a new one."}
            </DialogDescription>
          </DialogHeader>

          {!pwDone && (
            <form
              className="space-y-3"
              onSubmit={(e) => {
                e.preventDefault();
                void onSubmitPassword();
              }}
            >
              <Input
                type="password"
                autoComplete="current-password"
                placeholder="Current password"
                value={oldPw}
                onChange={(e) => setOldPw(e.target.value)}
                disabled={pwBusy}
                required
              />
              <Input
                type="password"
                autoComplete="new-password"
                placeholder="New password"
                value={newPw}
                onChange={(e) => setNewPw(e.target.value)}
                disabled={pwBusy}
                required
              />
              <Input
                type="password"
                autoComplete="new-password"
                placeholder="Confirm new password"
                value={confirmPw}
                onChange={(e) => setConfirmPw(e.target.value)}
                disabled={pwBusy}
                required
              />
              {pwError !== null && (
                <div
                  role="alert"
                  className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
                >
                  {pwError}
                </div>
              )}
              <DialogFooter>
                <Button
                  type="submit"
                  disabled={
                    pwBusy || oldPw.length === 0 || newPw.length === 0 || confirmPw.length === 0
                  }
                >
                  {pwBusy ? "Changing…" : "Change password"}
                </Button>
              </DialogFooter>
            </form>
          )}

          {pwDone && (
            <DialogFooter>
              <Button onClick={() => setPwOpen(false)}>Done</Button>
            </DialogFooter>
          )}
        </DialogContent>
      </Dialog>
    </Section>
  );
}

function ArchivedSection() {
  // includeArchived:true is the only way to load archived rows; the
  // default sidebar query no longer surfaces them.
  const query = useConversations("", true);
  const archived = useMemo(
    () => (query.data?.pages ?? []).flatMap((p) => p.data).filter((c) => c.archived === true),
    [query.data],
  );

  return (
    <Section
      title="Archived sessions"
      description="Sessions you've archived. Restore one to the sidebar, or delete it for good."
    >
      {query.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : archived.length === 0 ? (
        <p className="text-sm text-muted-foreground">No archived sessions.</p>
      ) : (
        <ul className="flex flex-col gap-0.5">
          {archived.map((conv) => (
            <ArchivedRow key={conv.id} conversation={conv} />
          ))}
        </ul>
      )}
    </Section>
  );
}

/**
 * One archived-session row. Not clickable (archived sessions aren't a
 * navigation target here); the title + timestamp read as a record, and the
 * Delete / Unarchive controls reveal on hover (always visible on touch).
 */
function ArchivedRow({ conversation }: { conversation: Conversation }) {
  const archive = useArchiveConversation();
  const del = useStopAndDeleteConversation();
  const [deleteOpen, setDeleteOpen] = useState(false);
  const label = conversationDisplayLabel(conversation);
  const busy = archive.isPending || del.isPending;

  return (
    <li
      data-testid="archived-row"
      className="group relative flex items-center gap-2 rounded-md px-3 py-2 hover:bg-muted"
    >
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium" title={label}>
          {label}
        </div>
        <div className="text-xs text-muted-foreground">
          {absoluteTime(conversation.updated_at * 1000)}
        </div>
      </div>
      {/* Actions reveal on hover (desktop) / always shown on touch. */}
      <div className="flex shrink-0 items-center gap-1 transition-opacity md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100">
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Delete session"
          data-testid="delete-archived"
          disabled={busy}
          onClick={() => setDeleteOpen(true)}
        >
          <Trash2Icon className="size-4 text-destructive" />
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          // No background in light mode (ghost). Dark mode needs a fill so the
          // button reads against the dark row — borrow the secondary tokens
          // there only, without touching the text color.
          className="gap-1.5 dark:bg-secondary dark:hover:bg-secondary/80"
          data-testid="unarchive-conversation"
          disabled={busy}
          onClick={() => archive.mutate({ id: conversation.id, archived: false })}
        >
          <ArchiveRestoreIcon className="size-3.5" />
          Unarchive
        </Button>
      </div>

      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete session?</DialogTitle>
            <DialogDescription>
              <span className="font-medium break-all">{label}</span> and all of its history will be
              removed. This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setDeleteOpen(false)} disabled={del.isPending}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={del.isPending}
              onClick={() => {
                // Fire-and-forget: the row drops out once the conversations
                // cache refreshes after the delete settles.
                del.mutate({ id: conversation.id });
                setDeleteOpen(false);
              }}
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </li>
  );
}
