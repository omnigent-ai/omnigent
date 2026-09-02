/**
 * Settings page (``/settings``).
 *
 * Renders into the AppShell chat outlet (see App.tsx) so the conversations
 * sidebar stays put when you enter settings — only the main area swaps to
 * this view. Inside, a section nav (left) drives a content panel (right),
 * modeled on a desktop-app settings window; a Back link
 * returns to the composer.
 *
 * Sections:
 *
 * - **General** — app-wide behavior preferences.
 * - **Appearance** — theme mode (System / Light / Dark), terminal theme,
 *   default transcript view, Workspace panel default, and UI/code font controls.
 * - **Git** — Git behavior: the global "always use a random worktree" default
 *   and the default base branch pre-filled when naming a new worktree branch.
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
 *   list. Not clickable; each row reveals Delete / Unarchive on hover, and
 *   Unarchive opens the restored session.
 */

import {
  lazy,
  type CSSProperties,
  type ReactNode,
  Suspense,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ArchiveRestoreIcon,
  AlertTriangleIcon,
  DownloadIcon,
  KeyRoundIcon,
  Loader2Icon,
  LaptopMinimalIcon,
  LogOutIcon,
  MessagesSquareIcon,
  MinusIcon,
  MonitorIcon,
  MoonIcon,
  PanelRightCloseIcon,
  PanelRightIcon,
  PlusIcon,
  SunIcon,
  SquareCheckIcon,
  SquareIcon,
  TerminalIcon,
  Trash2Icon,
  UploadIcon,
  UserCogIcon,
  XIcon,
  ClockIcon,
} from "lucide-react";
import { useTheme } from "next-themes";
import { PageScroll } from "@/components/PageScroll";
import { ThemeColorPicker } from "@/components/theme/ThemeColorPicker";
import { CardRadioGroup } from "@/components/theme/CardRadioGroup";
import {
  ModePreview,
  PaletteChip,
  PaletteSwatchPreview,
} from "@/components/theme/AppearancePreviews";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { MOD_KEY } from "@/components/KeyboardShortcut";
import { KeyboardShortcutsList } from "@/components/KeyboardShortcutsDialog";
import { changePassword, logout } from "@/lib/accountsApi";
import { getCurrentIsAdmin, getCurrentUserId, resolveIdentity } from "@/lib/identity";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { useOmnigentAnalytics, useOmnigentPageView } from "@/lib/analytics";
import {
  type Conversation,
  useArchiveConversation,
  useArchivedProjectNames,
  useBulkArchiveConversations,
  useBulkDeleteConversations,
  useConversations,
  useStopAndDeleteConversation,
} from "@/hooks/useConversations";
import { conversationDisplayLabel } from "@/shell/sidebarNav";
import { absoluteTime } from "@/lib/relativeTime";
import { useNavigate } from "@/lib/routing";
import { useSettingsRoute } from "@/shell/settingsNav";
import { ImportSessionsPanel } from "@/shell/ImportSessionsPanel";
import { isThemeMode, normalizeThemeMode, type ThemeMode } from "@/components/theme/themeMode";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import {
  applyDesktopUiFontSize,
  applyUiFontFamily,
  clampUiFontSizePx,
  readUiFontFamily,
  readUiFontSizePx,
  UI_FONT_FAMILY_DEFAULT,
  UI_FONT_SIZE_DEFAULT,
  UI_FONT_SIZE_MAX,
  UI_FONT_SIZE_MIN,
  UI_FONT_SIZE_STEP,
  writeUiFontFamily,
  writeUiFontSizePx,
} from "@/lib/uiFontPreferences";
import {
  clampCodeFontSizePx,
  CODE_FONT_FAMILY_DEFAULT,
  CODE_FONT_SIZE_DEFAULT,
  CODE_FONT_SIZE_MAX,
  CODE_FONT_SIZE_MIN,
  CODE_FONT_SIZE_STEP,
  CODE_FONT_WEIGHT_DEFAULT,
  CODE_FONT_WEIGHT_HEAVIER,
  CODE_FONT_WEIGHT_NORMAL,
  readCodeFontFamily,
  readCodeFontSizePx,
  readCodeFontWeight,
  writeCodeFontFamily,
  writeCodeFontSizePx,
  writeCodeFontWeight,
} from "@/lib/codeFontPreferences";
import {
  readTerminalThemeMode,
  TERMINAL_THEME_DEFAULT,
  writeTerminalThemeMode,
  type TerminalThemeMode,
} from "@/lib/terminalThemePreferences";
import {
  readWorkspacePanelDefault,
  WORKSPACE_PANEL_DEFAULT,
  writeWorkspacePanelDefault,
  type WorkspacePanelDefault,
} from "@/lib/workspacePanelPreferences";
import {
  readTranscriptViewDefault,
  TRANSCRIPT_VIEW_DEFAULT,
  writeTranscriptViewDefault,
  type TranscriptViewDefault,
} from "@/lib/transcriptViewPreferences";
import { readDefaultBaseBranch, writeDefaultBaseBranch } from "@/lib/baseBranchPreferences";
import { readAlwaysSteer, writeAlwaysSteer } from "@/lib/alwaysSteerPreferences";
import {
  readSubmitWithModEnter,
  writeSubmitWithModEnter,
} from "@/lib/composerSendShortcutPreferences";
import { readAlwaysUseWorktree, writeAlwaysUseWorktree } from "@/lib/worktreeDefaultPreferences";
import {
  archivedAtSeconds,
  readRetentionDays,
  writeRetentionDays,
} from "@/lib/retentionPreferences";
import {
  DEFAULT_HIDE_UNCONFIGURED_HARNESSES,
  readHideUnconfiguredHarnesses,
  writeHideUnconfiguredHarnesses,
} from "@/lib/harnessVisibilityPreferences";
import {
  applyThemePalette,
  DEFAULT_PALETTE,
  isThemeSelection,
  PALETTES,
  readThemePalette,
  type ThemeSelection,
  writeThemePalette,
} from "@/lib/themePalette";
import {
  applyCustomTheme,
  createCustomThemeFromPalette,
  customThemeSwatches,
  DEFAULT_CUSTOM_THEME,
  readCustomTheme,
  type CustomTheme,
  writeCustomTheme,
} from "@/lib/customTheme";
import { useIsEmbedded } from "@/lib/embedded";
import { getOmnigentThemeSettingsUrl } from "@/lib/host";
import {
  applyImportedSettings,
  collectSettings,
  downloadSettings,
  readSettingsFile,
} from "@/lib/settingsPortability";
import {
  type CliStatus,
  getCliStatus,
  isElectronShell,
  resetCliPath,
  type UpdateConfig,
  type UpdateMode,
  updateBridge,
} from "@/lib/nativeBridge";
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
const SharingPage = lazy(() =>
  import("@/pages/SharingPage").then((m) => ({ default: m.SharingPage })),
);

/**
 * The current viewer's user id, resolved reactively. Uses `getCurrentUserId`
 * (NOT `getCurrentAuthorId`): ownership compares against the session's `owner`
 * grant, which in single-user mode is the reserved `"local"` id — and
 * `getCurrentAuthorId` nulls `"local"` out (it's for author labels), which
 * would make the viewer's own sessions read as shared and vanish from the
 * default "My sessions" tab. `getCurrentUserId` keeps `"local"` and is the
 * identical real email in multi-user mode. It is synchronous (populated once
 * `resolveIdentity` has run — which `main.tsx` kicks off at boot), but on a
 * cold mount it can still be null for a tick, so we also await
 * `resolveIdentity()` and re-render when it lands. Keeping this reactive
 * (rather than a bare module read) means the My/Shared split settles correctly
 * the moment identity is known, without a manual refresh.
 */
function useViewerId(): string | null {
  const [viewerId, setViewerId] = useState<string | null>(() => getCurrentUserId());
  useEffect(() => {
    let cancelled = false;
    void resolveIdentity().then(() => {
      if (!cancelled) setViewerId(getCurrentUserId());
    });
    return () => {
      cancelled = true;
    };
  }, []);
  return viewerId;
}

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
  // Per-section page view: `settings.appearance`, `settings.account`, etc. The
  // hook re-keys on pathname, so switching sections re-fires under the new id.
  // `section` is a closed SettingsSectionId union (no PII / unbounded values).
  useOmnigentPageView(`settings.${section}`);

  // Members / Policies are admin-only management surfaces that own their full
  // layout (their own PageScroll + admin gating), so they render directly —
  // NOT inside the shared section PageScroll below, which would nest two
  // scroll containers. Both self-gate to admins server-side and client-side.
  // Rendered in ANY multi-user mode (accounts AND OIDC), not gated on
  // `accountsEnabled` — the nav + pages handle admin gating, and Members runs
  // read-only under OIDC (no password actions).
  if (section === "members" || section === "policies" || section === "sharing") {
    return (
      <Suspense fallback={null}>
        {section === "members" ? (
          <MembersPage />
        ) : section === "policies" ? (
          <PoliciesPage />
        ) : (
          <SharingPage />
        )}
      </Suspense>
    );
  }

  return (
    <PageScroll contentClassName="px-8" extraBottom="2.5rem">
      {section === "appearance" && <AppearanceSection />}
      {section === "general" && <GeneralSection />}
      {section === "git" && <GitSection />}
      {section === "shortcuts" && <ShortcutsSection />}
      {section === "import" && <ImportSection />}
      {section === "account" && hasAuthSession && <AccountSection />}
      {section === "archived" && <ArchivedSection />}
      {section === "cli" && isElectronShell() && <LocalCliSection />}
      {section === "updates" && isElectronShell() && <UpdatesSection />}
    </PageScroll>
  );
}

/** Shared section shell: a title + optional description above the body. */
function Section({
  title,
  description,
  descriptionClassName,
  children,
}: {
  title: string;
  description?: string;
  descriptionClassName?: string;
  children: ReactNode;
}) {
  return (
    <section>
      <h1 className="text-2xl font-semibold">{title}</h1>
      {description && (
        <p className={cn("mt-1 text-muted-foreground", descriptionClassName ?? "text-ui")}>
          {description}
        </p>
      )}
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

const transcriptViewCards: {
  value: TranscriptViewDefault;
  label: string;
  icon: typeof MessagesSquareIcon;
}[] = [
  { value: "chat", label: "Chat", icon: MessagesSquareIcon },
  { value: "terminal", label: "Terminal", icon: TerminalIcon },
];

const workspacePanelCards: {
  value: WorkspacePanelDefault;
  label: string;
  icon: typeof PanelRightIcon;
}[] = [
  { value: "open", label: "Open", icon: PanelRightIcon },
  { value: "collapsed", label: "Collapsed", icon: PanelRightCloseIcon },
];

/** Centered icon + label body shared by the Mode and Terminal theme cards. */
function iconCardBody(Icon: typeof SunIcon, label: string) {
  return (
    <>
      <Icon className="size-6 text-muted-foreground" />
      <span className="text-ui font-medium">{label}</span>
    </>
  );
}

/** A labeled Appearance subsection: heading + one-line helper + its control. */
function ThemeSubsection({
  labelId,
  title,
  helper,
  children,
}: {
  labelId: string;
  title: string;
  helper: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col">
        <span id={labelId} className="text-ui font-medium">
          {title}
        </span>
        <span className="text-sm text-muted-foreground">{helper}</span>
      </div>
      {children}
    </div>
  );
}

/** Appearance mode: System / Light / Dark. */
function ModeControl() {
  const { theme, setTheme } = useTheme();
  const mode = normalizeThemeMode(theme);
  const labelId = useId();
  return (
    <ThemeSubsection
      labelId={labelId}
      title="Mode"
      helper="Follow your system, or force light or dark."
    >
      <CardRadioGroup<ThemeMode>
        labelledBy={labelId}
        value={mode}
        onSelect={(next) => setTheme(next)}
        componentId="settings.appearance.theme_mode"
        className="grid grid-cols-3 gap-3"
        cardClassName="gap-2 p-2"
        items={themeCards.map((card) => ({
          value: card.mode,
          testId: `theme-${card.mode}`,
          body: (
            <>
              <ModePreview variant={card.mode} />
              <span className="text-center text-ui font-medium">{card.label}</span>
            </>
          ),
        }))}
      />
    </ThemeSubsection>
  );
}

/** Terminal light/dark/match-app theme — its own section. */
function TerminalThemeControl() {
  const [mode, setMode] = useState(() => readTerminalThemeMode());
  const labelId = useId();
  const choose = useCallback((next: TerminalThemeMode) => {
    setMode(next);
    writeTerminalThemeMode(next);
  }, []);
  return (
    <ThemeSubsection
      labelId={labelId}
      title="Terminal theme"
      helper="Use a light or dark terminal, or match the app."
    >
      <CardRadioGroup<TerminalThemeMode>
        labelledBy={labelId}
        value={mode}
        onSelect={choose}
        componentId="settings.appearance.terminal_theme"
        className="grid grid-cols-3 gap-3"
        cardClassName="items-center gap-2 p-4"
        items={terminalThemeCards.map((card) => ({
          value: card.mode,
          testId: `terminal-theme-${card.mode}`,
          body: iconCardBody(card.icon, card.label),
        }))}
      />
    </ThemeSubsection>
  );
}

/** Default surface for terminal-first transcripts without a per-tab choice. */
function TranscriptViewDefaultControl() {
  const [value, setValue] = useState(() => readTranscriptViewDefault());
  const labelId = useId();
  const choose = useCallback((next: TranscriptViewDefault) => {
    setValue(next);
    writeTranscriptViewDefault(next);
  }, []);
  return (
    <ThemeSubsection
      labelId={labelId}
      title="Default transcript view"
      helper="Choose whether terminal-backed chats open in Chat or Terminal view. A view selected in a chat is remembered for the current tab."
    >
      <CardRadioGroup<TranscriptViewDefault>
        labelledBy={labelId}
        value={value}
        onSelect={choose}
        componentId="settings.appearance.transcript_view"
        className="grid grid-cols-2 gap-3"
        cardClassName="items-center gap-2 p-4"
        items={transcriptViewCards.map((card) => ({
          value: card.value,
          testId: `transcript-view-default-${card.value}`,
          body: iconCardBody(card.icon, card.label),
        }))}
      />
    </ThemeSubsection>
  );
}

/**
 * Default open/collapsed state for the right Workspace rail on brand-new chats.
 * Only applies when a session has no saved per-chat open state — existing
 * sessions keep restoring whatever the user last left them as.
 */
function WorkspacePanelDefaultControl() {
  const [value, setValue] = useState(() => readWorkspacePanelDefault());
  const labelId = useId();
  const choose = useCallback((next: WorkspacePanelDefault) => {
    setValue(next);
    writeWorkspacePanelDefault(next);
  }, []);
  return (
    <ThemeSubsection
      labelId={labelId}
      title="Workspace panel"
      helper="Whether new chats open with the Files / Agents / Shells panel visible. Collapsing or expanding the panel updates this. Existing chats keep their last layout."
    >
      <CardRadioGroup<WorkspacePanelDefault>
        labelledBy={labelId}
        value={value}
        onSelect={choose}
        componentId="settings.appearance.workspace_panel"
        className="grid grid-cols-2 gap-3"
        cardClassName="items-center gap-2 p-4"
        items={workspacePanelCards.map((card) => ({
          value: card.value,
          testId: `workspace-panel-default-${card.value}`,
          body: iconCardBody(card.icon, card.label),
        }))}
      />
    </ThemeSubsection>
  );
}

function ColorThemeControl() {
  // Render each chip in the currently-resolved mode so it matches the app now
  // (honoring the embed's forced theme, not just next-themes' resolvedTheme).
  const isDark = useResolvedThemeMode() === "dark";
  const [selection, setSelection] = useState<ThemeSelection>(() => readThemePalette());
  const [customTheme, setCustomTheme] = useState<CustomTheme>(() => readCustomTheme());
  const labelId = useId();

  const choose = useCallback(
    (next: ThemeSelection) => {
      if (next === "custom") applyCustomTheme(customTheme);
      setSelection(next);
      writeThemePalette(next);
      applyThemePalette(next);
    },
    [customTheme],
  );

  const selectedPalette =
    selection === "custom"
      ? null
      : (PALETTES.find((palette) => palette.id === selection) ?? PALETTES[0]);
  const editableTheme = selectedPalette
    ? createCustomThemeFromPalette(selectedPalette)
    : customTheme;
  const customSwatches = customThemeSwatches(customTheme);

  const updateCustomTheme = useCallback(
    (patch: Partial<CustomTheme>) => {
      const source =
        selection === "custom"
          ? customTheme
          : createCustomThemeFromPalette(
              PALETTES.find((palette) => palette.id === selection) ?? PALETTES[0],
            );
      const next = { ...source, ...patch };
      setCustomTheme(next);
      writeCustomTheme(next);
      applyCustomTheme(next);
      setSelection("custom");
      writeThemePalette("custom");
      applyThemePalette("custom");
    },
    [customTheme, selection],
  );

  const selected =
    selection === "custom"
      ? {
          label: "Custom",
          light: customSwatches.light,
          dark: customSwatches.dark,
        }
      : selectedPalette!;

  return (
    <ThemeSubsection
      labelId={labelId}
      title="Color theme"
      helper="Choose a preset, then tune it across light and dark mode."
    >
      <div className="overflow-hidden rounded-xl border bg-card/55 shadow-xs">
        <div className="flex flex-col gap-3 border-b bg-muted/30 p-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 items-center gap-3">
            <div className="w-28 shrink-0 overflow-hidden rounded-lg shadow-sm">
              <PaletteSwatchPreview swatch={isDark ? selected.dark : selected.light} />
            </div>
            <div className="min-w-0">
              <div className="text-ui font-medium">Theme palette</div>
              <div className="truncate text-sm text-muted-foreground">
                {selection === "custom"
                  ? `Based on ${PALETTES.find((palette) => palette.id === customTheme.basePalette)?.label ?? "Omnigent"}`
                  : selectedPalette?.blurb}
              </div>
            </div>
          </div>
          <Select
            value={selection}
            onValueChange={(next) => {
              if (isThemeSelection(next)) choose(next);
            }}
            componentId="settings.appearance.color_theme"
            valueHasNoPii
          >
            <SelectTrigger
              aria-labelledby={labelId}
              data-testid="color-theme-select"
              className="w-full gap-2 sm:w-48"
            >
              <SelectValue>
                <PaletteChip swatch={isDark ? selected.dark : selected.light} />
                <span>{selected.label}</span>
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              {PALETTES.map((palette) => (
                <SelectItem
                  key={palette.id}
                  value={palette.id}
                  data-testid={`palette-${palette.id}`}
                >
                  <PaletteChip swatch={isDark ? palette.dark : palette.light} />
                  <span>{palette.label}</span>
                </SelectItem>
              ))}
              <SelectItem value="custom" data-testid="palette-custom">
                <PaletteChip swatch={isDark ? customSwatches.dark : customSwatches.light} />
                <span>Custom</span>
              </SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div className="px-4">
          <ThemeColorPicker
            label="Accent"
            value={editableTheme.accent}
            testId="custom-theme-accent"
            onChange={(accent) => updateCustomTheme({ accent, darkAccent: accent })}
          />
          <ThemeColorPicker
            label="Background tint"
            value={editableTheme.tint}
            testId="custom-theme-tint"
            onChange={(tint) => updateCustomTheme({ tint })}
          />
          <div className="flex items-center justify-between gap-4 border-b border-border/70 py-4">
            <div>
              <div className="text-ui font-medium">Contrast</div>
              <div className="text-sm text-muted-foreground">
                Separates text, borders, and surfaces.
              </div>
            </div>
            <div className="flex w-52 items-center gap-3">
              <input
                id="custom-theme-contrast"
                type="range"
                min="0"
                max="100"
                value={editableTheme.contrast}
                aria-label="Theme contrast"
                data-testid="custom-theme-contrast"
                onChange={(event) => updateCustomTheme({ contrast: Number(event.target.value) })}
                className="theme-contrast-range min-w-0 flex-1 cursor-pointer"
                style={{ "--range-progress": `${editableTheme.contrast}%` } as CSSProperties}
              />
              <output
                htmlFor="custom-theme-contrast"
                data-testid="custom-theme-contrast-value"
                className="w-7 text-right text-sm font-medium tabular-nums"
              >
                {editableTheme.contrast}
              </output>
            </div>
          </div>
          <div className="flex items-center justify-between gap-4 py-4">
            <div>
              <div className="text-ui font-medium">Translucent sidebars</div>
              <div className="text-sm text-muted-foreground">
                Lets the canvas show through the conversation and workspace rails.
              </div>
            </div>
            <Switch
              aria-label="Translucent sidebars"
              checked={editableTheme.translucentSidebar}
              onCheckedChange={(translucentSidebar) => updateCustomTheme({ translucentSidebar })}
              data-testid="custom-theme-translucent-sidebar"
              componentId="settings.appearance.translucent_sidebar"
            />
          </div>
        </div>
      </div>
    </ThemeSubsection>
  );
}

/**
 * Opt-in filter for the new-chat harness picker: when on, harnesses that
 * aren't set up on the selected host (missing CLI / auth) are hidden instead
 * of badged. Off by default so the picker keeps surfacing harnesses to set up.
 * Fails open — with no connected host or readiness info, nothing is hidden.
 */
function HideUnconfiguredHarnessesControl() {
  const [value, setValue] = useState(() => readHideUnconfiguredHarnesses());
  const labelId = useId();
  const toggle = useCallback((next: boolean) => {
    setValue(next);
    writeHideUnconfiguredHarnesses(next);
  }, []);
  return (
    <div className="flex items-start justify-between gap-6">
      <div className="flex flex-col">
        <span id={labelId} className="text-ui font-medium">
          Hide unconfigured harnesses
        </span>
        <span className="text-sm text-muted-foreground">
          Only show harnesses that are set up on the selected host in the new-chat picker. Harnesses
          needing a CLI install or sign-in are hidden instead of badged.
        </span>
      </div>
      <Switch
        aria-labelledby={labelId}
        checked={value}
        onCheckedChange={toggle}
        data-testid="hide-unconfigured-harnesses-toggle"
        className="mt-0.5 shrink-0"
        componentId="settings.appearance.hide_unconfigured_harnesses"
      />
    </div>
  );
}

function AppearanceSection() {
  // Embedded: the host owns light/dark, so the Mode picker would be a no-op —
  // replace it with a note (plus a link to the host's own theme settings when
  // one is provided). The color palette, terminal theme, and font controls are
  // per-device prefs that don't conflict with host light/dark, so they stay.
  const isEmbedded = useIsEmbedded();
  const themeSettingsUrl = getOmnigentThemeSettingsUrl();
  const { setTheme } = useTheme();
  const [resetKey, setResetKey] = useState(0);
  const [isResetDialogOpen, setIsResetDialogOpen] = useState(false);
  const [isImportDialogOpen, setIsImportDialogOpen] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const resetAppearance = () => {
    // Reset every appearance preference back to the product default.
    setTheme("system");

    writeTerminalThemeMode(TERMINAL_THEME_DEFAULT);

    writeThemePalette(DEFAULT_PALETTE);
    applyThemePalette(DEFAULT_PALETTE);
    writeCustomTheme(DEFAULT_CUSTOM_THEME);
    applyCustomTheme(DEFAULT_CUSTOM_THEME);

    writeTranscriptViewDefault(TRANSCRIPT_VIEW_DEFAULT);

    writeWorkspacePanelDefault(WORKSPACE_PANEL_DEFAULT);

    writeHideUnconfiguredHarnesses(DEFAULT_HIDE_UNCONFIGURED_HARNESSES);

    applyDesktopUiFontSize(UI_FONT_SIZE_DEFAULT);
    applyUiFontFamily(UI_FONT_FAMILY_DEFAULT);

    writeCodeFontSizePx(CODE_FONT_SIZE_DEFAULT);
    writeCodeFontFamily(CODE_FONT_FAMILY_DEFAULT);
    writeCodeFontWeight(CODE_FONT_WEIGHT_DEFAULT);

    // Remove the persisted keys so this device has no appearance overrides at
    // all. Some write helpers already remove the key for the default value;
    // clearing the list here makes the intent explicit and keeps the reset
    // behavior consistent even if a helper changes later.
    if (typeof window !== "undefined") {
      try {
        for (const key of [
          "omnigent:ui-font-size",
          "omnigent:ui-font-family",
          "omnigent:code-font-size",
          "omnigent:code-font-family",
          "omnigent:code-font-weight",
          "omnigent:terminal-theme",
          "omnigent:ui-theme-palette",
          "omnigent:custom-theme",
          "omnigent:default-transcript-view",
          "omnigent:default-workspace-panel",
          "omnigent:hide-unconfigured-harnesses",
        ]) {
          window.localStorage.removeItem(key);
        }
      } catch {
        // localStorage access errors are non-fatal.
      }
    }

    // Remount the controls so they re-read the freshly-cleared defaults from
    // localStorage rather than keeping their stale seeded state.
    setResetKey((k) => k + 1);
  };

  const confirmResetAppearance = () => {
    resetAppearance();
    setIsResetDialogOpen(false);
  };

  const exportSettings = () => {
    const exported = collectSettings();
    if (exported) downloadSettings(exported);
  };

  const handleImportFile = async (file: File) => {
    setImportError(null);
    try {
      const imported = await readSettingsFile(file);
      applyImportedSettings(imported);

      // Apply DOM side-effects so imported settings take effect immediately.
      // Note: web-theme is stored as plain string by next-themes, not JSON.
      const themeMode = imported.settings["web-theme"];
      if (themeMode && isThemeMode(themeMode)) setTheme(themeMode);
      applyDesktopUiFontSize(readUiFontSizePx());
      applyUiFontFamily(readUiFontFamily());
      applyThemePalette(readThemePalette());
      applyCustomTheme(readCustomTheme());

      setIsImportDialogOpen(false);
      setResetKey((k) => k + 1);
    } catch (err) {
      setImportError(err instanceof Error ? err.message : "Import failed.");
    }
  };

  return (
    <Section
      title="Appearance"
      description="Choose how Omnigent looks on this device."
      descriptionClassName="text-sm"
    >
      <div key={resetKey} className="flex flex-col gap-8">
        {isEmbedded ? (
          <div className="flex flex-col gap-3">
            <span className="text-ui font-medium">Theme</span>
            <p className="text-sm text-muted-foreground">
              Light and dark mode are configured in Databricks preferences.
              {themeSettingsUrl ? (
                <>
                  {" "}
                  <a
                    href={themeSettingsUrl}
                    className="font-medium text-primary underline underline-offset-2 hover:text-primary/80"
                  >
                    Click to open Databricks user preferences page.
                  </a>
                  .
                </>
              ) : null}
            </p>
          </div>
        ) : (
          <ModeControl />
        )}

        <TerminalThemeControl />

        <ColorThemeControl />

        <TranscriptViewDefaultControl />

        <WorkspacePanelDefaultControl />

        <HideUnconfiguredHarnessesControl />

        <UiFontSizeControl />

        <UiFontFamilyControl />

        {/* Code font (Monaco + xterm) sits as its own rows — labelled in full
            ("Code font size" / "Code font family" / "Code font weight") rather than under a shared
            heading — so each control reads unambiguously next to the UI-font rows
            above and it's clear these don't scale the surrounding chrome. */}
        <UiCodeFontSizeControl />

        <UiCodeFontFamilyControl />

        <UiCodeFontWeightControl />
      </div>

      <div className="mt-8 flex items-center justify-end gap-2">
        <Button
          variant="outline"
          size="sm"
          data-testid="export-settings-button"
          onClick={exportSettings}
        >
          <DownloadIcon className="size-4" />
          Export
        </Button>
        <Button
          variant="outline"
          size="sm"
          data-testid="import-settings-button"
          onClick={() => {
            setImportError(null);
            setIsImportDialogOpen(true);
          }}
        >
          <UploadIcon className="size-4" />
          Import
        </Button>
        <Dialog open={isResetDialogOpen} onOpenChange={setIsResetDialogOpen}>
          <DialogTrigger asChild>
            <Button
              variant="outline"
              size="sm"
              data-testid="reset-appearance-button"
              componentId="settings.appearance.open_reset_dialog"
            >
              Reset to defaults
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Reset appearance?</DialogTitle>
              <DialogDescription>
                This will reset every appearance choice back to its default.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <DialogClose asChild>
                <Button variant="outline" size="sm">
                  Cancel
                </Button>
              </DialogClose>
              <Button
                variant="default"
                size="sm"
                onClick={confirmResetAppearance}
                data-testid="reset-appearance-confirm"
                componentId="settings.appearance.reset"
              >
                Reset
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>

      <input
        ref={fileInputRef}
        type="file"
        accept=".json"
        className="hidden"
        data-testid="import-settings-file-input"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) void handleImportFile(file);
          e.target.value = "";
        }}
      />
      <Dialog open={isImportDialogOpen} onOpenChange={setIsImportDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Import settings</DialogTitle>
            <DialogDescription>
              Choose an exported Omnigent settings file to apply. This will overwrite your current
              appearance and preference settings.
            </DialogDescription>
          </DialogHeader>
          {importError && (
            <div
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
            >
              {importError}
            </div>
          )}
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="outline" size="sm">
                Cancel
              </Button>
            </DialogClose>
            <Button
              variant="default"
              size="sm"
              data-testid="import-settings-choose-file"
              onClick={() => fileInputRef.current?.click()}
            >
              Choose file
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Section>
  );
}

/** Git behavior settings. */
function GitSection() {
  return (
    <Section title="Git" description="Configure how Omnigent works with Git.">
      <div className="flex flex-col gap-8">
        <AlwaysUseWorktreeControl />
        <DefaultBaseBranchControl />
      </div>
    </Section>
  );
}

/**
 * Global default: start every new session in a git workspace in a fresh
 * randomly-named worktree, regardless of which folder the composer lands in.
 * Per-project "Random worktree" settings override this in either direction —
 * this only decides the default for workspaces a project hasn't set a choice on.
 */
function AlwaysUseWorktreeControl() {
  const [value, setValue] = useState(() => readAlwaysUseWorktree());
  const labelId = useId();
  const toggle = useCallback((next: boolean) => {
    setValue(next);
    writeAlwaysUseWorktree(next);
  }, []);
  return (
    <div className="flex items-start justify-between gap-6">
      <div className="flex min-w-0 flex-1 flex-col">
        <span id={labelId} className="text-ui font-medium">
          Always use a random worktree
        </span>
        <span className="text-ui text-muted-foreground">
          Start new sessions in a fresh randomly-named git worktree in any git workspace. A
          project's own Random worktree setting overrides this.
        </span>
      </div>
      <Switch
        aria-labelledby={labelId}
        checked={value}
        onCheckedChange={toggle}
        data-testid="settings-always-use-worktree-toggle"
        className="mt-0.5 shrink-0"
        componentId="settings.git.always_use_worktree"
      />
    </div>
  );
}

/**
 * Opt-in dispatch for messages sent while the agent is working.
 */
function AlwaysSteerControl() {
  const [value, setValue] = useState(() => readAlwaysSteer());
  const labelId = useId();
  const toggle = useCallback((next: boolean) => {
    setValue(next);
    writeAlwaysSteer(next);
  }, []);
  return (
    <div className="flex items-start justify-between gap-6">
      <div className="flex min-w-0 flex-1 flex-col">
        <span id={labelId} className="text-ui font-medium">
          Always steer
        </span>
        <span className="text-ui text-muted-foreground">
          Send follow-ups straight into the running turn instead of queuing them. The agent folds
          each one into its current work where the harness supports it, otherwise at the next turn.
        </span>
      </div>
      <Switch
        aria-labelledby={labelId}
        checked={value}
        onCheckedChange={toggle}
        data-testid="always-steer-toggle"
        className="mt-0.5 shrink-0"
        componentId="settings.general.always_steer"
      />
    </div>
  );
}

function ComposerSendShortcutControl() {
  const [enabled, setEnabled] = useState(() => readSubmitWithModEnter());
  const labelId = useId();
  const descriptionId = useId();
  const toggle = useCallback((next: boolean) => {
    setEnabled(next);
    writeSubmitWithModEnter(next);
  }, []);

  return (
    <div className="flex items-start justify-between gap-6">
      <div className="flex min-w-0 flex-1 flex-col">
        <span id={labelId} className="text-ui font-medium">
          Submit with {MOD_KEY} + Enter on desktop
        </span>
        <div id={descriptionId} className="text-ui text-muted-foreground">
          <p>Off: Enter submits and Shift+Enter inserts a newline.</p>
          <p>On: Enter inserts a newline and {MOD_KEY}+Enter submits.</p>
        </div>
      </div>
      <Switch
        aria-labelledby={labelId}
        aria-describedby={descriptionId}
        checked={enabled}
        onCheckedChange={toggle}
        data-testid="composer-submit-with-mod-enter-toggle"
        className="mt-0.5 shrink-0"
        componentId="settings.general.submit_with_mod_enter"
      />
    </div>
  );
}

/** App-wide behavior settings. */
function GeneralSection() {
  return (
    <Section title="General" description="Configure general Omnigent behavior.">
      <div className="flex flex-col gap-3">
        <h2 className="text-ui font-medium">Composer</h2>
        <div className="rounded-xl border border-border bg-card p-4">
          <ComposerSendShortcutControl />
          <div className="mt-4 border-t border-border pt-4">
            <AlwaysSteerControl />
          </div>
        </div>
      </div>
    </Section>
  );
}

/**
 * Default base branch for new worktrees. When set, the new-session composer
 * pre-fills the base-branch field as you name a new branch, so the worktree
 * branches off it. Leave blank to keep the field empty (worktrees default to
 * the current branch).
 */
function DefaultBaseBranchControl() {
  const [branch, setBranch] = useState(() => readDefaultBaseBranch() ?? "");

  const update = useCallback((next: string) => {
    setBranch(next);
    writeDefaultBaseBranch(next);
  }, []);

  return (
    <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="text-ui font-medium">Default base branch</span>
        <span className="text-ui text-muted-foreground">
          Auto-filled as the base when you name a new worktree branch. Leave blank to not auto-fill.
        </span>
      </div>
      <Input
        type="text"
        aria-label="Default base branch"
        data-testid="settings-default-base-branch-input"
        placeholder="e.g. main"
        spellCheck={false}
        autoCapitalize="off"
        autoCorrect="off"
        className="h-9 w-56 shrink-0"
        value={branch}
        onChange={(e) => update(e.target.value)}
        componentId="settings.git.default_branch"
      />
    </div>
  );
}

/**
 * UI font size stepper. Maps one of the supported discrete px values into
 * typography tokens via --desktop-ui-font-size (see lib/uiFontPreferences.ts)
 * without resizing layout or icons. Desktop reads the value directly; mobile
 * scales its own base from it, so the setting applies on both surfaces.
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
    applyDesktopUiFontSize(clamped);
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
        applyDesktopUiFontSize(value);
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
        <span className="text-ui font-medium">Interface font size</span>
        <span className="text-sm text-muted-foreground">
          Set text across the interface. Icons and spacing stay fixed.
        </span>
      </div>
      {/* One cohesive pill: [ −  | value px |  + ]. Segments share the pill
          border via inner dividers rather than floating as separate boxes. */}
      <div
        role="group"
        aria-label="Interface font size"
        className={cn(
          "inline-flex h-9 items-stretch overflow-hidden rounded-lg border border-input bg-background transition-colors dark:bg-input/30",
          "focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50",
        )}
      >
        <StepperButton
          label="Decrease interface font size"
          testId="ui-font-size-dec"
          disabled={atMin}
          onClick={() => commit(px - UI_FONT_SIZE_STEP)}
          componentId="settings.appearance.ui_font_decrease"
        >
          <MinusIcon className="ui-icon" />
        </StepperButton>
        <div className="flex items-center border-x border-input px-2 tabular-nums">
          <input
            type="number"
            inputMode="numeric"
            min={UI_FONT_SIZE_MIN}
            max={UI_FONT_SIZE_MAX}
            step={UI_FONT_SIZE_STEP}
            aria-label="Interface font size in pixels"
            data-testid="ui-font-size-input"
            className="w-8 bg-transparent text-center text-ui font-medium tabular-nums outline-none [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
            value={draft}
            onChange={(e) => onDraftChange(e.target.value)}
            onBlur={commitDraft}
            onKeyDown={(e) => {
              if (e.key === "Enter") e.currentTarget.blur();
            }}
          />
        </div>
        <StepperButton
          label="Increase interface font size"
          testId="ui-font-size-inc"
          disabled={atMax}
          onClick={() => commit(px + UI_FONT_SIZE_STEP)}
          componentId="settings.appearance.ui_font_increase"
        >
          <PlusIcon className="ui-icon" />
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
        <span className="text-ui font-medium">Font family</span>
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
          componentId="settings.appearance.ui_font_family_reset"
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
 * — fixed-pixel widgets that don't inherit the desktop UI typography tokens, so
 * writing the pref emits to already-mounted editors/terminals (see
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
        <span className="text-ui font-medium">Code font size</span>
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
          componentId="settings.appearance.code_font_decrease"
        >
          <MinusIcon className="ui-icon" />
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
            className="w-8 bg-transparent text-center text-ui font-medium tabular-nums outline-none [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
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
          componentId="settings.appearance.code_font_increase"
        >
          <PlusIcon className="ui-icon" />
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
        <span className="text-ui font-medium">Code font family</span>
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
          componentId="settings.appearance.code_font_family_reset"
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

/** Font weight preset shared by Monaco and xterm code surfaces. */
function UiCodeFontWeightControl() {
  const [heavier, setHeavier] = useState(() => readCodeFontWeight() === CODE_FONT_WEIGHT_HEAVIER);

  const toggle = (enabled: boolean) => {
    setHeavier(enabled);
    writeCodeFontWeight(enabled ? CODE_FONT_WEIGHT_HEAVIER : CODE_FONT_WEIGHT_NORMAL);
  };

  return (
    <div className="flex items-center justify-between gap-6" data-testid="code-font-weight-control">
      <div className="min-w-0 flex-1">
        <span className="text-ui font-medium">Heavier code font</span>
        <span className="block text-sm text-muted-foreground">
          Use a slightly heavier font weight in the code editor and terminal.
        </span>
      </div>
      <Switch
        aria-label="Use heavier code text"
        checked={heavier}
        onCheckedChange={toggle}
        data-testid="heavier-code-text-toggle"
        className="shrink-0"
        componentId="settings.appearance.heavier_code_text"
      />
    </div>
  );
}

/** Flanking +/- segment of the font-size pill: square, ghost-hover, no border. */
function StepperButton({
  label,
  testId,
  disabled,
  onClick,
  componentId,
  children,
}: {
  label: string;
  testId: string;
  disabled: boolean;
  onClick: () => void;
  componentId?: string;
  children: ReactNode;
}) {
  const { trackClick } = useOmnigentAnalytics();
  return (
    <button
      type="button"
      aria-label={label}
      data-testid={testId}
      disabled={disabled}
      onClick={() => {
        if (componentId) trackClick(componentId, "button");
        onClick();
      }}
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
        <p className="text-ui text-muted-foreground">Checking…</p>
      </Section>
    );
  }

  return (
    <Section
      title="Local CLI"
      description="The Omnigent command-line tool this app uses to run a local server and connect this machine as a runner."
    >
      {status === null ? (
        <p className="text-ui text-muted-foreground">CLI status is unavailable.</p>
      ) : (
        <div className="flex flex-col gap-4">
          <div className="flex items-center gap-2 text-ui">
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
              <span className="text-sm text-muted-foreground">
                {status.source === "configured" ? "Path (custom)" : "Path (auto-detected)"}
              </span>
              <code className="block overflow-x-auto rounded-md border border-border bg-muted/40 px-3 py-2 text-sm">
                {status.path}
              </code>
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              <p className="text-ui text-muted-foreground">
                The Omnigent CLI wasn't found. Install it, then set its path from the connect
                screen:
              </p>
              {status.installCommand && (
                <code className="block overflow-x-auto rounded-md border border-border bg-muted/40 px-3 py-2 text-sm">
                  {status.installCommand}
                </code>
              )}
            </div>
          )}

          {status.customizationDisabled ? (
            <p className="text-sm text-muted-foreground">
              Managed by your organization. Host enrollment uses <code>isaac omni</code>.
            </p>
          ) : (
            <>
              <p className="text-sm text-muted-foreground">
                For security, a custom path can only be set from the connect screen — this prevents
                a connected server from pointing the app at a different binary. Open it from the
                Server menu (Change Server…) and use the settings gear.
              </p>

              {status.source === "configured" && (
                <div>
                  <Button variant="ghost" size="sm" disabled={busy} onClick={() => void onReset()}>
                    Reset to auto-detected
                  </Button>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </Section>
  );
}

const UPDATE_MODE_LABELS: Record<UpdateMode, string> = {
  default: "Automatic (check periodically, ask before installing)",
  start: "Check when Omnigent starts",
  manual: "Manual only",
  none: "Off",
};

function UpdatesSection() {
  const bridge = updateBridge();
  const [config, setConfig] = useState<UpdateConfig | null | "loading">("loading");
  const [saving, setSaving] = useState(false);
  const [checking, setChecking] = useState(false);
  const [lastCheckError, setLastCheckError] = useState<string | null>(null);

  useEffect(() => {
    if (!bridge) {
      setConfig(null);
      return undefined;
    }
    let alive = true;
    void bridge
      .getConfig()
      .then((nextConfig) => {
        if (alive) setConfig(nextConfig);
      })
      .catch((err) => {
        console.warn("[SettingsPage] update config read failed:", err);
        if (alive) setConfig(null);
      });
    const unsubscribe = bridge.onStatus((status) => {
      if (status.state === "error-security") {
        setLastCheckError(status.lastError ?? "Security verification failed.");
      } else if (status.state === "idle" && status.lastError) {
        setLastCheckError(status.lastError);
      } else if (
        status.state === "checking" ||
        status.state === "available" ||
        status.state === "none"
      ) {
        setLastCheckError(null);
      }
    });
    return () => {
      alive = false;
      unsubscribe();
    };
  }, [bridge]);

  const persistConfig = useCallback(
    async (patch: Partial<UpdateConfig>) => {
      if (!bridge) return;
      setSaving(true);
      try {
        const next = await bridge.setConfig(patch);
        setConfig(next);
      } finally {
        setSaving(false);
      }
    },
    [bridge],
  );

  const onCheck = useCallback(async () => {
    if (!bridge) return;
    setChecking(true);
    setLastCheckError(null);
    try {
      await bridge.check();
    } catch (err) {
      setLastCheckError(err instanceof Error ? err.message : String(err));
    } finally {
      setChecking(false);
    }
  }, [bridge]);

  if (config === "loading") {
    return (
      <Section title="Updates">
        <p className="text-ui text-muted-foreground">Checking…</p>
      </Section>
    );
  }

  return (
    <Section
      title="Updates"
      description="Desktop app update preferences for this installed Omnigent shell."
    >
      {config === null ? (
        <p className="text-ui text-muted-foreground">Update settings are unavailable.</p>
      ) : (
        <div className="flex max-w-2xl flex-col gap-5">
          <label className="flex flex-col gap-2">
            <span className="text-ui font-medium">Update mode</span>
            <Select
              value={config.mode}
              onValueChange={(value) => void persistConfig({ mode: value as UpdateMode })}
              disabled={saving}
              componentId="settings.updates.mode"
              valueHasNoPii
            >
              <SelectTrigger className="w-full max-w-md" data-testid="update-mode-select">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(Object.keys(UPDATE_MODE_LABELS) as UpdateMode[]).map((mode) => (
                  <SelectItem key={mode} value={mode}>
                    {UPDATE_MODE_LABELS[mode]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>

          <div className="flex items-center justify-between gap-4 rounded-lg border border-border px-4 py-3">
            <div className="flex flex-col gap-1">
              <span className="text-ui font-medium">Install downloaded updates on next quit</span>
              <span className="text-sm text-muted-foreground">
                Applies only after you choose to download an update.
              </span>
            </div>
            <Switch
              checked={config.autoInstall}
              onCheckedChange={(checked) => void persistConfig({ autoInstall: checked })}
              disabled={saving}
              aria-label="Install downloaded updates on next quit"
              componentId="settings.updates.auto_install"
            />
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <Button
              onClick={() => void onCheck()}
              loading={checking}
              componentId="settings.updates.check_now"
            >
              Check for updates now
            </Button>
            {saving && <span className="text-sm text-muted-foreground">Saving…</span>}
          </div>

          {lastCheckError && (
            <div className="flex items-start gap-2 rounded-lg border border-border bg-muted/50 px-3 py-2 text-ui">
              <AlertTriangleIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
              <div>
                <div className="font-medium">Last check failed</div>
                <div className="text-muted-foreground">{lastCheckError}</div>
              </div>
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
                <span className="ml-1 text-sm font-normal text-muted-foreground">(admin)</span>
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
              componentId="settings.account.change_password"
            >
              <KeyRoundIcon className="size-4" /> Change password
            </Button>
          )}
          <Button
            variant="ghost"
            className="w-full justify-start gap-2"
            onClick={() => void onSignOut()}
            componentId="settings.account.sign_out"
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
                  className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-ui text-destructive"
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
                  componentId="settings.account.update_password"
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

// Discriminated Select values so the "no filter" sentinel can never collide
// with a real project name: the reset option is a fixed token that no project
// value can equal, and every project is namespaced under a prefix so its name
// carries through verbatim. A project literally named "all" (or "__all__")
// therefore still filters correctly instead of clearing the filter.
const ALL_PROJECTS_VALUE = "all";
const PROJECT_VALUE_PREFIX = "project:";

function projectToSelectValue(project: string | undefined): string {
  return project === undefined ? ALL_PROJECTS_VALUE : PROJECT_VALUE_PREFIX + project;
}

function selectValueToProject(value: string): string | undefined {
  if (value === ALL_PROJECTS_VALUE) return undefined;
  return value.slice(PROJECT_VALUE_PREFIX.length);
}

function dateGroupLabel(timestampSec: number, now: Date = new Date()): string {
  const date = new Date(timestampSec * 1000);
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());

  const yesterday = new Date(startOfToday);
  yesterday.setDate(yesterday.getDate() - 1);

  const sevenDaysAgo = new Date(startOfToday);
  sevenDaysAgo.setDate(sevenDaysAgo.getDate() - 7);

  const thirtyDaysAgo = new Date(startOfToday);
  thirtyDaysAgo.setDate(thirtyDaysAgo.getDate() - 30);

  if (date >= startOfToday) return "Today";
  if (date >= yesterday) return "Yesterday";
  if (date >= sevenDaysAgo) return "Previous 7 days";
  if (date >= thirtyDaysAgo) return "Previous 30 days";
  return date.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

const RETENTION_OPTIONS: { label: string; value: string; days: number | null }[] = [
  { label: "Never", value: "never", days: null },
  { label: "After 7 days", value: "7", days: 7 },
  { label: "After 30 days", value: "30", days: 30 },
  { label: "After 60 days", value: "60", days: 60 },
  { label: "After 90 days", value: "90", days: 90 },
];

function retentionDaysToSelectValue(days: number | null): string {
  if (days === null) return "never";
  return String(days);
}

function selectValueToRetentionDays(value: string): number | null {
  if (value === "never") return null;
  const parsed = parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function ImportSection() {
  return (
    <Section
      title="Import sessions"
      description="Pull your recent local chats from a machine you're running into Omnigent. Sessions already imported are skipped."
    >
      <ImportSessionsPanel />
    </Section>
  );
}

function ArchivedSection() {
  // `undefined` = all projects; a name scopes the list to that project.
  const [project, setProject] = useState<string | undefined>(undefined);
  const [retentionDays, setRetentionDays] = useState<number | null>(() => readRetentionDays());
  const [deleteExpiredOpen, setDeleteExpiredOpen] = useState(false);
  const bulkDelete = useBulkDeleteConversations();
  const viewerId = useViewerId();

  // Picker options: every project that has an archived session. Sourced from a
  // dedicated hook that pages through ALL archived sessions server-side —
  // `useProjects()` omits all-archived projects, and deriving options from only
  // the visible list's loaded first page would hide archived-only projects
  // whose sessions sit on later pages.
  const namesQuery = useArchivedProjectNames();
  const projectNames = useMemo(() => namesQuery.data ?? [], [namesQuery.data]);

  // A picked project can vanish from the option set for good (its last
  // archived session deleted or restored, possibly by another client). Once
  // the scan settles without it, fall back to "All projects" rather than
  // pinning a defunct filter with a project-scoped empty state.
  useEffect(() => {
    if (
      project !== undefined &&
      namesQuery.isSuccess &&
      !namesQuery.isFetching &&
      !projectNames.includes(project)
    ) {
      setProject(undefined);
    }
  }, [project, projectNames, namesQuery.isSuccess, namesQuery.isFetching]);

  // The visible list, filtered server-side via ?project= when one is picked.
  const listQuery = useConversations("", true, undefined, project);
  const archived = useMemo(
    () => (listQuery.data?.pages ?? []).flatMap((p) => p.data).filter((c) => c.archived === true),
    [listQuery.data],
  );

  const cutoff = useMemo(() => {
    if (retentionDays === null) return null;
    return Math.floor(Date.now() / 1000) - retentionDays * 86400;
  }, [retentionDays]);

  const expiredSessions = useMemo(() => {
    if (cutoff === null) return [];
    return archived.filter((c) => archivedAtSeconds(c) < cutoff);
  }, [archived, cutoff]);

  // Filter expired sessions to only owned ones (same pattern as ArchivedBulkActionBar)
  const ownedExpiredSessions = useMemo(() => {
    return expiredSessions.filter((c) => {
      const owner = c.owner;
      return owner === null || owner === viewerId;
    });
  }, [expiredSessions, viewerId]);

  const groupedArchived = useMemo(() => {
    const now = new Date();
    const groups: { label: string; conversations: typeof archived }[] = [];
    let currentLabel = "";
    for (const conv of archived) {
      const label = dateGroupLabel(conv.updated_at, now);
      if (label !== currentLabel) {
        currentLabel = label;
        groups.push({ label, conversations: [] });
      }
      groups[groups.length - 1].conversations.push(conv);
    }
    return groups;
  }, [archived]);

  // Keep a picked project listed even if it drops out of the option set (its
  // last archived session was just unarchived) so the trigger never shows a
  // blank, orphaned value while the refetch settles.
  const items =
    project && !projectNames.includes(project) ? [project, ...projectNames] : projectNames;

  // ── Bulk selection ──
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  const toggleSelected = useCallback((id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const selectAll = useCallback(() => {
    setSelectedIds(new Set(archived.map((c) => c.id)));
  }, [archived]);

  const deselectAll = useCallback(() => {
    setSelectedIds(new Set());
  }, []);

  const exitSelectionMode = useCallback(() => {
    setSelectionMode(false);
    setSelectedIds(new Set());
  }, []);

  // Prune stale selections when archived list changes (rows deleted/unarchived).
  useEffect(() => {
    setSelectedIds((prev) => {
      const ids = new Set(archived.map((c) => c.id));
      const next = new Set([...prev].filter((id) => ids.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [archived]);

  return (
    <Section
      title="Archived sessions"
      description="Sessions you've archived. Restore one to the sidebar, or delete it for good."
    >
      <div className="mb-4 flex flex-wrap items-center gap-x-6 gap-y-2">
        <div className="flex items-center gap-2">
          <label htmlFor="archived-retention" className="text-ui text-muted-foreground">
            Mark as expired after
          </label>
          <Select
            value={retentionDaysToSelectValue(retentionDays)}
            onValueChange={(value) => {
              const days = selectValueToRetentionDays(value);
              setRetentionDays(days);
              writeRetentionDays(days);
            }}
          >
            <SelectTrigger
              id="archived-retention"
              aria-label="Mark archived sessions as expired after"
              data-testid="archived-retention"
              className="w-40"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent position="popper" align="start">
              {RETENTION_OPTIONS.map((opt) => (
                <SelectItem key={opt.value} value={opt.value}>
                  {opt.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        {items.length > 0 && (
          <div className="flex items-center gap-2">
            <label htmlFor="archived-project-filter" className="text-ui text-muted-foreground">
              Project
            </label>
            <Select
              value={projectToSelectValue(project)}
              onValueChange={(value) => setProject(selectValueToProject(value))}
            >
              <SelectTrigger
                id="archived-project-filter"
                aria-label="Filter archived sessions by project"
                data-testid="archived-project-filter"
                className="w-56"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper" align="start">
                <SelectItem value={ALL_PROJECTS_VALUE}>All projects</SelectItem>
                {items.map((name) => (
                  <SelectItem
                    key={name}
                    value={projectToSelectValue(name)}
                    data-testid={`archived-project-option-${name}`}
                  >
                    {name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}
        {!selectionMode && archived.length > 0 && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            data-testid="archived-toggle-selection"
            onClick={() => setSelectionMode(true)}
          >
            Select
          </Button>
        )}
      </div>

      {selectionMode && (
        <ArchivedBulkActionBar
          selectedIds={selectedIds}
          allArchived={archived}
          onSelectAll={selectAll}
          onDeselectAll={deselectAll}
          onExit={exitSelectionMode}
        />
      )}

      {ownedExpiredSessions.length > 0 && (
        <div className="mb-4 flex items-center gap-3 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2">
          <ClockIcon className="size-4 shrink-0 text-destructive" />
          <span className="text-ui flex-1">
            {ownedExpiredSessions.length === 1
              ? "1 expired session"
              : `${ownedExpiredSessions.length} expired sessions`}{" "}
            {listQuery.hasNextPage ? "on loaded pages " : ""}past the {retentionDays}-day retention
            period.
          </span>
          <Button
            type="button"
            variant="destructive"
            size="sm"
            data-testid="delete-expired"
            disabled={bulkDelete.isPending}
            onClick={() => setDeleteExpiredOpen(true)}
          >
            Delete expired
          </Button>
          <Dialog open={deleteExpiredOpen} onOpenChange={setDeleteExpiredOpen}>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>Delete expired sessions?</DialogTitle>
                <DialogDescription>
                  {ownedExpiredSessions.length === 1
                    ? "1 owned archived session"
                    : `${ownedExpiredSessions.length} owned archived sessions`}{" "}
                  older than {retentionDays} days {listQuery.hasNextPage ? "on loaded pages " : ""}
                  will be permanently deleted. This cannot be undone.
                  {listQuery.hasNextPage && (
                    <span className="mt-2 block text-sm">
                      Note: More archived sessions may exist on unfetched pages. Click "Load more"
                      to see all expired sessions before deleting.
                    </span>
                  )}
                </DialogDescription>
              </DialogHeader>
              <DialogFooter>
                <Button
                  variant="ghost"
                  onClick={() => setDeleteExpiredOpen(false)}
                  disabled={bulkDelete.isPending}
                >
                  Cancel
                </Button>
                <Button
                  variant="destructive"
                  disabled={bulkDelete.isPending}
                  onClick={() => {
                    bulkDelete.mutate({ ids: ownedExpiredSessions.map((c) => c.id) });
                    setDeleteExpiredOpen(false);
                  }}
                >
                  Delete{" "}
                  {ownedExpiredSessions.length === 1
                    ? "1 session"
                    : `${ownedExpiredSessions.length} sessions`}
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>
      )}

      {listQuery.isLoading ? (
        <p className="text-ui text-muted-foreground">Loading…</p>
      ) : archived.length === 0 && !listQuery.hasNextPage ? (
        // Definitive empty only when there are no archived rows AND no further
        // pages to fetch.
        <p className="text-ui text-muted-foreground">
          {project ? "No archived sessions in this project." : "No archived sessions."}
        </p>
      ) : (
        <>
          {archived.length > 0 && (
            <div className="flex flex-col gap-4">
              {groupedArchived.map((group) => (
                <div key={group.label}>
                  <h3 className="mb-1 px-3 text-sm font-medium text-muted-foreground">
                    {group.label}
                  </h3>
                  <ul className="flex flex-col gap-0.5">
                    {group.conversations.map((conv) => (
                      <ArchivedRow
                        key={conv.id}
                        conversation={conv}
                        cutoff={cutoff}
                        selectionMode={selectionMode}
                        isSelected={selectedIds.has(conv.id)}
                        onToggleSelected={toggleSelected}
                      />
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          )}
          {archived.length === 0 && (
            // The list fetches a mixed page (active + archived rows) and filters
            // to archived client-side; archived sessions are older and can sort
            // onto later pages, so a page with none isn't the end. Offer to page
            // forward instead of dead-ending on the definitive empty state.
            <p className="text-ui text-muted-foreground">
              {project
                ? "No archived sessions in this project on this page."
                : "No archived sessions on this page."}
            </p>
          )}
          {/* Keep the pager visible whenever more pages exist, independent of the
              current page's archived count — otherwise a first page of only
              active rows would hide the archived rows on later pages. */}
          {listQuery.hasNextPage && (
            <div className="mt-3">
              <Button
                type="button"
                variant="ghost"
                size="sm"
                data-testid="archived-load-more"
                disabled={listQuery.isFetchingNextPage}
                onClick={() => void listQuery.fetchNextPage()}
              >
                {listQuery.isFetchingNextPage ? "Loading…" : "Load more"}
              </Button>
            </div>
          )}
        </>
      )}
    </Section>
  );
}

/**
 * Bulk action bar for the archived-sessions settings section. Modeled on the
 * sidebar's BulkActionBar but scoped to archived rows — offers Delete and
 * Unarchive, plus Select all / Deselect all / exit controls.
 */
function ArchivedBulkActionBar({
  selectedIds,
  allArchived,
  onSelectAll,
  onDeselectAll,
  onExit,
}: {
  selectedIds: Set<string>;
  allArchived: Conversation[];
  onSelectAll: () => void;
  onDeselectAll: () => void;
  onExit: () => void;
}) {
  const bulkArchive = useBulkArchiveConversations();
  const bulkDelete = useBulkDeleteConversations();
  const viewerId = useViewerId();

  const ownedSelected = useMemo(() => {
    return allArchived.filter((c) => {
      if (!selectedIds.has(c.id)) return false;
      const owner = c.owner ?? null;
      return owner === null || owner === viewerId;
    });
  }, [allArchived, selectedIds, viewerId]);

  const count = selectedIds.size;
  const allSelected = count > 0 && count === allArchived.length;
  const isBusy = bulkArchive.isPending || bulkDelete.isPending;
  const [confirmDeleteOpen, setConfirmDeleteOpen] = useState(false);

  function handleUnarchive() {
    if (ownedSelected.length === 0) return;
    bulkArchive.mutate(
      { ids: ownedSelected.map((c) => c.id), archived: false },
      { onSuccess: onDeselectAll },
    );
  }

  function handleDelete() {
    const ids = ownedSelected.map((c) => c.id);
    if (ids.length === 0) return;
    setConfirmDeleteOpen(false);
    bulkDelete.mutate({ ids }, { onSuccess: onDeselectAll });
  }

  return (
    <>
      <div className="relative mb-4 flex flex-col gap-1.5 rounded-md border bg-muted/50 p-2">
        <div className="relative flex min-h-8 items-center gap-1.5 pr-9">
          <span className="shrink-0 whitespace-nowrap text-sm text-muted-foreground">
            {count === 0 ? "None selected" : `${count} selected`}
          </span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-6 px-1.5 text-sm"
            onClick={allSelected ? onDeselectAll : onSelectAll}
          >
            {allSelected ? "Deselect all" : "Select all"}
          </Button>
          <Button
            type="button"
            variant="secondary"
            size="icon-sm"
            className="-translate-y-1/2 absolute top-1/2 right-1 shrink-0 rounded-full"
            aria-label="Exit selection mode"
            data-testid="archived-exit-selection"
            onClick={onExit}
          >
            <XIcon className="size-3.5" />
          </Button>
        </div>

        <div className="flex items-center gap-1.5">
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-7 gap-1.5 text-xs"
            disabled={isBusy || ownedSelected.length === 0}
            onClick={handleUnarchive}
            data-testid="archived-bulk-unarchive"
          >
            {bulkArchive.isPending ? (
              <Loader2Icon className="size-3 animate-spin" />
            ) : (
              <ArchiveRestoreIcon className="size-3" />
            )}
            Unarchive {ownedSelected.length > 0 ? ownedSelected.length : ""}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className={cn("h-7 gap-1.5 text-xs", ownedSelected.length > 0 && "text-destructive")}
            disabled={isBusy || ownedSelected.length === 0}
            onClick={() => setConfirmDeleteOpen(true)}
            data-testid="archived-bulk-delete"
          >
            {bulkDelete.isPending ? (
              <Loader2Icon className="size-3 animate-spin" />
            ) : (
              <Trash2Icon className="size-3" />
            )}
            Delete {ownedSelected.length > 0 ? ownedSelected.length : ""}
          </Button>
        </div>

        {(bulkArchive.isError || bulkDelete.isError) && (
          <p className="text-xs text-destructive" role="alert">
            Some actions failed. Retry or dismiss.
          </p>
        )}
      </div>

      <Dialog open={confirmDeleteOpen} onOpenChange={setConfirmDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete {ownedSelected.length} session(s)?</DialogTitle>
            <DialogDescription>
              This will permanently delete the selected sessions and all their history. This cannot
              be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              onClick={() => setConfirmDeleteOpen(false)}
              disabled={bulkDelete.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={handleDelete}
              disabled={bulkDelete.isPending}
            >
              Delete {ownedSelected.length} session(s)
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

/**
 * One archived-session row. Not clickable (archived sessions aren't a
 * navigation target here); the title + timestamp read as a record, and the
 * Delete / Unarchive controls reveal on hover (always visible on touch).
 * In selection mode, clicking the row toggles its checkbox.
 * Unarchive navigates to the restored session once the PATCH lands.
 */
function ArchivedRow({
  conversation,
  cutoff,
  selectionMode,
  isSelected,
  onToggleSelected,
}: {
  conversation: Conversation;
  cutoff: number | null;
  selectionMode: boolean;
  isSelected: boolean;
  onToggleSelected: (id: string) => void;
}) {
  const isExpired = cutoff !== null && archivedAtSeconds(conversation) < cutoff;
  const navigate = useNavigate();
  const archive = useArchiveConversation();
  const del = useStopAndDeleteConversation();
  const [deleteOpen, setDeleteOpen] = useState(false);
  const label = conversationDisplayLabel(conversation);
  const busy = archive.isPending || del.isPending;

  return (
    <li
      data-testid="archived-row"
      className={cn(
        "group relative flex items-center gap-2 rounded-md px-3 py-2 hover:bg-muted",
        selectionMode && "cursor-pointer",
        isSelected && "bg-muted",
      )}
      onClick={selectionMode ? () => onToggleSelected(conversation.id) : undefined}
    >
      {selectionMode && (
        <span className="flex shrink-0 items-center">
          {isSelected ? (
            <SquareCheckIcon className="size-4 text-primary" />
          ) : (
            <SquareIcon className="size-4 text-muted-foreground" />
          )}
        </span>
      )}
      <div className="min-w-0 flex-1">
        <div className="truncate text-ui font-medium" title={label}>
          {label}
        </div>
        <div className="flex items-center gap-1.5 text-sm text-muted-foreground">
          <span>{absoluteTime(conversation.updated_at * 1000)}</span>
          {isExpired && (
            <span className="rounded bg-destructive/10 px-1.5 py-0.5 text-xs font-medium text-destructive">
              Expired
            </span>
          )}
        </div>
      </div>
      {/* Actions reveal on hover (desktop) / always shown on touch.
          Hidden in selection mode — bulk bar owns the actions. */}
      {!selectionMode && (
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
            onClick={() =>
              archive.mutate(
                { id: conversation.id, archived: false },
                { onSuccess: () => navigate(`/c/${conversation.id}`) },
              )
            }
          >
            <ArchiveRestoreIcon className="size-3.5" />
            Unarchive
          </Button>
        </div>
      )}

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
