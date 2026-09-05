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
  type ComponentType,
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
  CheckIcon,
  ClockIcon,
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
} from "lucide-react";
import { useTheme } from "next-themes";
import { PageScroll } from "@/components/PageScroll";
import {
  ArchiveLibraryToolbar,
  buildArchiveConversationFilters,
  type ArchiveFilterOption,
  type ArchiveLibraryViewState,
} from "@/components/archive/ArchiveLibraryToolbar";
import { ArchiveTranscriptViewer } from "@/components/archive/ArchiveTranscriptViewer";
import { ThemeColorPicker } from "@/components/theme/ThemeColorPicker";
import { CardRadioGroup } from "@/components/theme/CardRadioGroup";
import {
  ModePreview,
  PaletteChip,
  PaletteSwatchPreview,
} from "@/components/theme/AppearancePreviews";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
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
import {
  beginGithubConnect,
  disconnectGithub,
  fetchGithubStatus,
  type GithubConnectionStatus,
} from "@/lib/githubIntegration";
import { getCurrentIsAdmin, getCurrentUserId, resolveIdentity } from "@/lib/identity";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { useOmnigentAnalytics, useOmnigentPageView } from "@/lib/analytics";
import {
  type Conversation,
  type ArchivedDateField,
  useArchiveConversation,
  useArchivedConversations,
  useArchivedSessionFacets,
  useBulkArchiveConversations,
  useBulkDeleteConversations,
  useProjects,
  useStopAndDeleteConversation,
} from "@/hooks/useConversations";
import { useHosts } from "@/hooks/useHosts";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useResizableColumn } from "@/hooks/useResizableColumn";
import { conversationDisplayLabel } from "@/shell/sidebarNav";
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

  if (section === "archived") return <ArchivedSection />;

  return (
    <PageScroll contentClassName="px-8" extraBottom="2.5rem">
      {section === "appearance" && <AppearanceSection />}
      {section === "general" && <GeneralSection />}
      {section === "git" && <GitSection />}
      {section === "integrations" && <IntegrationsSection />}
      {section === "shortcuts" && <ShortcutsSection />}
      {section === "import" && <ImportSection />}
      {section === "account" && hasAuthSession && <AccountSection />}
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

/** GitHub brand mark (lucide dropped brand icons, so inline the glyph). */
function GithubMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" aria-hidden className={className} fill="currentColor">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
    </svg>
  );
}

/**
/**
 * Which panel connects/disconnects each provider. The server's
 * ``enabled_connections`` list says WHICH panels to show; this map says HOW to
 * render each. Adding a provider is one entry here plus one string server-side.
 */
const CONNECTION_PANELS: Record<string, ComponentType> = {
  github: GithubIntegrationControl,
};

/**
 * Sandbox Integrations settings. Renders one connect/disconnect panel per
 * provider the server reports in ``enabled_connections``, in that order. The
 * nav hides the section entirely when the list is empty.
 */
function IntegrationsSection() {
  const info = useServerInfo();
  const providers = info === "loading" ? [] : (info.enabled_connections ?? []);
  return (
    <Section
      title="Sandbox Integrations"
      description="External accounts your sandboxes use on your behalf."
    >
      {providers.map((provider) => {
        const Panel = CONNECTION_PANELS[provider];
        return Panel ? <Panel key={provider} /> : null;
      })}
    </Section>
  );
}

/**
 * Connect / disconnect a GitHub account. Once connected, a managed
 * sandbox launched by this user authenticates ``gh`` / git as them and
 * receives their public SSH keys (so they can SSH into their own box).
 * The connect action is a full-page redirect to GitHub; on return the
 * callback lands back here with ``?github=connected|error``.
 */
function GithubIntegrationControl() {
  const [status, setStatus] = useState<GithubConnectionStatus | null | "loading">("loading");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<"connected" | "error" | null>(null);

  const refresh = useCallback(async () => {
    try {
      setStatus(await fetchGithubStatus());
    } catch {
      setStatus(null);
    }
  }, []);

  useEffect(() => {
    void refresh();
    // Surface the callback outcome carried back in the URL, then strip it
    // so a reload doesn't re-show the banner.
    const params = new URLSearchParams(window.location.search);
    const outcome = params.get("github");
    if (outcome === "connected" || outcome === "error") {
      setNotice(outcome);
      params.delete("github");
      const qs = params.toString();
      window.history.replaceState({}, "", `${window.location.pathname}${qs ? `?${qs}` : ""}`);
    }
  }, [refresh]);

  const onDisconnect = useCallback(async () => {
    setBusy(true);
    try {
      await disconnectGithub();
      await refresh();
    } finally {
      setBusy(false);
    }
  }, [refresh]);

  const returnTo = `${window.location.pathname}${window.location.search}`;

  if (status === "loading") {
    return <p className="text-sm text-muted-foreground">Checking…</p>;
  }
  if (status === null) {
    return <p className="text-sm text-muted-foreground">GitHub status is unavailable.</p>;
  }

  return (
    <div className="flex flex-col gap-4">
      {notice === "connected" && (
        <div
          role="status"
          className="rounded-md border border-success/40 bg-success/10 px-3 py-2 text-sm"
        >
          GitHub account connected.
        </div>
      )}
      {notice === "error" && (
        <div
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          Couldn't connect your GitHub account. Please try again.
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
        <div className="flex min-w-0 flex-1 flex-col">
          <span className="text-sm font-medium">GitHub</span>
          <span className="text-sm text-muted-foreground">
            {status.connected && status.login
              ? `Connected as ${status.login}. New sandboxes authenticate gh and git as you, and your public SSH keys are added so you can SSH in.`
              : "Connect your GitHub account so new sandboxes authenticate gh and git as you, and your public SSH keys are injected."}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {status.connected ? (
            <Button
              variant="ghost"
              size="sm"
              className="h-9"
              disabled={busy}
              data-testid="github-disconnect"
              onClick={() => void onDisconnect()}
            >
              Disconnect
            </Button>
          ) : (
            <Button
              size="sm"
              className="h-9 gap-2"
              disabled={busy}
              data-testid="github-connect"
              onClick={() => beginGithubConnect(returnTo)}
            >
              <GithubMark className="size-4" />
              Connect GitHub
            </Button>
          )}
        </div>
      </div>

      {status.install_url && (
        <p className="text-xs text-muted-foreground">
          The app may need to be installed on your repositories.{" "}
          <a
            href={status.install_url}
            target="_blank"
            rel="noreferrer"
            className="underline underline-offset-2"
          >
            Manage installation
          </a>
          .
        </p>
      )}
    </div>
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

const ARCHIVED_VIEW_STORAGE_KEY = "omnigent:archived-sessions-view-v1";

type ArchivedViewPreferences = ArchiveLibraryViewState;

const DEFAULT_ARCHIVED_VIEW: ArchivedViewPreferences = {
  searchQuery: "",
  searchScope: "title",
  project: undefined,
  hostId: undefined,
  agentName: undefined,
  dateField: "archived_at",
  dateRange: "",
  sortField: "archived_at",
  order: "desc",
};

function readArchivedViewPreferences(): ArchivedViewPreferences {
  try {
    const stored = JSON.parse(localStorage.getItem(ARCHIVED_VIEW_STORAGE_KEY) ?? "null") as
      | (Partial<ArchivedViewPreferences> & {
          createdRange?: string;
          archivedRange?: string;
        })
      | null;
    if (!stored) return DEFAULT_ARCHIVED_VIEW;
    const dateField =
      stored.dateField === "created_at" || stored.dateField === "active_at"
        ? stored.dateField
        : "archived_at";
    const legacyRange =
      typeof stored.archivedRange === "string" && stored.archivedRange
        ? stored.archivedRange
        : typeof stored.createdRange === "string"
          ? stored.createdRange
          : "";
    return {
      ...DEFAULT_ARCHIVED_VIEW,
      ...stored,
      searchScope: stored.searchScope === "content" ? "content" : "title",
      dateField,
      dateRange: typeof stored.dateRange === "string" ? stored.dateRange : legacyRange,
      sortField:
        stored.sortField === "created_at" || stored.sortField === "title"
          ? stored.sortField
          : "archived_at",
      order: stored.order === "asc" ? "asc" : "desc",
    };
  } catch {
    return DEFAULT_ARCHIVED_VIEW;
  }
}

function archivedTimestamp(
  conversation: Conversation,
  field: ArchivedDateField = "archived_at",
): number {
  if (field === "created_at") return conversation.created_at;
  // Older Servers expose the stable transition only through the legacy label.
  // Current Servers provide archived_at; archivedAtSeconds keeps that older
  // retention contract before falling back to mutable updated_at.
  return conversation.archived_at ?? archivedAtSeconds(conversation);
}

const RETENTION_OPTIONS: { label: string; value: string; days: number | null }[] = [
  { label: "Never", value: "never", days: null },
  { label: "After 7 days", value: "7", days: 7 },
  { label: "After 30 days", value: "30", days: 30 },
  { label: "After 60 days", value: "60", days: 60 },
  { label: "After 90 days", value: "90", days: 90 },
];

function retentionDaysToSelectValue(days: number | null): string {
  return days === null ? "never" : String(days);
}

function selectValueToRetentionDays(value: string): number | null {
  if (value === "never") return null;
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function ImportSection() {
  return (
    <Section
      title="Import sessions"
      description="Pull local chats from a machine you're running into Omnigent. Sessions already imported are skipped."
    >
      <ImportSessionsPanel />
    </Section>
  );
}

function ArchivedSection() {
  const isMobileViewport = useIsMobileViewport();
  const [selectedConversation, setSelectedConversation] = useState<Conversation | null>(null);
  const { width, containerRef, handleProps } = useResizableColumn(420, 300, 720);
  const [containerWidth, setContainerWidth] = useState<number | null>(null);
  const listFocusRef = useRef<HTMLDivElement>(null);
  const singlePane = isMobileViewport || (containerWidth !== null && containerWidth < 680);
  const listWidth =
    containerWidth === null ? width : Math.min(width, Math.max(300, containerWidth - 320));

  useEffect(() => {
    const node = containerRef.current;
    if (!node) return;
    const update = () => {
      const measured = node.getBoundingClientRect().width;
      if (measured > 0) setContainerWidth(measured);
    };
    update();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(update);
    observer.observe(node);
    return () => observer.disconnect();
  }, [containerRef]);

  useEffect(() => {
    if (singlePane) setSelectedConversation(null);
  }, [singlePane]);

  return (
    <div
      ref={(node) => {
        containerRef.current = node;
      }}
      className="flex h-full min-h-0 min-w-0 overflow-hidden pt-[calc(var(--omnigent-header-height)+var(--omnigent-inset-top))] pb-[var(--omnigent-inset-bottom)]"
      data-testid="archive-library"
    >
      <div
        ref={listFocusRef}
        data-testid="archive-list-pane"
        className={cn(
          "min-h-0 shrink-0 overflow-x-hidden overflow-y-auto",
          singlePane && selectedConversation ? "hidden" : singlePane && "w-full",
        )}
        style={singlePane ? undefined : { width: listWidth }}
      >
        <ArchivedListPane
          selectedConversationId={selectedConversation?.id ?? null}
          autoSelectFirst={!singlePane}
          onSelectConversation={setSelectedConversation}
        />
      </div>
      {!singlePane && (
        <div
          {...handleProps}
          aria-label="Resize archive session list"
          title="Drag to resize the archive list and conversation."
          className="group relative w-1 shrink-0 cursor-col-resize border-x border-transparent hover:bg-primary/10"
        >
          <span className="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-border group-hover:bg-primary/50" />
        </div>
      )}
      {(!singlePane || selectedConversation) && (
        <ArchiveTranscriptViewer
          conversation={selectedConversation}
          onBack={singlePane ? () => setSelectedConversation(null) : undefined}
          returnFocusRef={listFocusRef}
        />
      )}
    </div>
  );
}

function ArchivedListPane({
  selectedConversationId,
  autoSelectFirst,
  onSelectConversation,
}: {
  selectedConversationId: string | null;
  autoSelectFirst: boolean;
  onSelectConversation: (conversation: Conversation | null) => void;
}) {
  const [view, setView] = useState<ArchivedViewPreferences>(readArchivedViewPreferences);
  const [debouncedSearchQuery, setDebouncedSearchQuery] = useState(view.searchQuery ?? "");
  const [pageCursors, setPageCursors] = useState<(string | undefined)[]>([undefined]);
  const [retentionDays, setRetentionDays] = useState<number | null>(readRetentionDays);
  const [deleteExpiredOpen, setDeleteExpiredOpen] = useState(false);
  const bulkDelete = useBulkDeleteConversations();
  const viewerId = useViewerId();
  const paginationAnchorRef = useRef<HTMLElement>(null);
  const projectsQuery = useProjects();
  const hostsQuery = useHosts({ includeSandbox: true });
  const pageNumber = pageCursors.length;
  useEffect(() => {
    const timeout = window.setTimeout(
      () => setDebouncedSearchQuery((view.searchQuery ?? "").trim()),
      250,
    );
    return () => window.clearTimeout(timeout);
  }, [view.searchQuery]);
  const queryView = useMemo(
    () => buildArchiveConversationFilters(view, debouncedSearchQuery),
    [debouncedSearchQuery, view],
  );
  const facetsQuery = useArchivedSessionFacets(queryView);
  const listQuery = useArchivedConversations(queryView, pageCursors.at(-1));
  const archived = useMemo(() => listQuery.data?.data ?? [], [listQuery.data]);
  const retentionCutoff = useMemo(
    () => (retentionDays === null ? null : Math.floor(Date.now() / 1000) - retentionDays * 86_400),
    [retentionDays],
  );
  const ownedExpiredSessions = useMemo(
    () =>
      retentionCutoff === null
        ? []
        : archived.filter((conversation) => {
            if (archivedTimestamp(conversation) >= retentionCutoff) return false;
            const owner = conversation.owner ?? null;
            return owner === null || owner === viewerId;
          }),
    [archived, retentionCutoff, viewerId],
  );

  useEffect(() => {
    if (listQuery.isLoading) return;
    if (archived.length === 0) {
      if (selectedConversationId !== null) onSelectConversation(null);
      return;
    }
    if (selectedConversationId === null) {
      if (autoSelectFirst) onSelectConversation(archived[0]);
      return;
    }
    const selected = archived.find((conversation) => conversation.id === selectedConversationId);
    onSelectConversation(selected ?? null);
  }, [
    archived,
    autoSelectFirst,
    listQuery.isLoading,
    onSelectConversation,
    selectedConversationId,
  ]);

  useEffect(() => {
    localStorage.setItem(ARCHIVED_VIEW_STORAGE_KEY, JSON.stringify(view));
  }, [view]);

  const updateView = useCallback((patch: Partial<ArchivedViewPreferences>) => {
    setPageCursors([undefined]);
    setView((current) => ({ ...current, ...patch }));
  }, []);

  const hostNames = useMemo(
    () => new Map((hostsQuery.data ?? []).map((host) => [host.host_id, host.name])),
    [hostsQuery.data],
  );
  const projectOptions = useMemo(() => {
    return (facetsQuery.data?.projects ?? []).map((name) => ({ value: name, label: name }));
  }, [facetsQuery.data]);
  const hostOptions = useMemo(
    () =>
      (facetsQuery.data?.hostIds ?? []).map((hostId) => ({
        value: hostId,
        label: hostNames.get(hostId) ?? hostId,
        keywords: hostId,
      })),
    [facetsQuery.data, hostNames],
  );
  const agentOptions = useMemo(
    () => (facetsQuery.data?.agentNames ?? []).map((name) => ({ value: name, label: name })),
    [facetsQuery.data],
  );
  const projectNames = useMemo(
    () => new Map((projectsQuery.data ?? []).map((project) => [project.id, project.name])),
    [projectsQuery.data],
  );

  useEffect(() => {
    if (!facetsQuery.data) return;
    const normalize = (key: "project" | "hostId" | "agentName", options: ArchiveFilterOption[]) => {
      const current = view[key];
      if (current && !options.some((option) => option.value === current)) {
        updateView({ [key]: undefined });
      }
    };
    normalize("project", projectOptions);
    normalize("hostId", hostOptions);
    normalize("agentName", agentOptions);
  }, [agentOptions, facetsQuery.data, hostOptions, projectOptions, updateView, view]);

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

  useEffect(() => {
    if (
      pageCursors.length > 1 &&
      listQuery.data !== undefined &&
      !listQuery.isFetching &&
      archived.length === 0
    ) {
      setPageCursors((current) => current.slice(0, -1));
    }
  }, [archived.length, listQuery.data, listQuery.isFetching, pageCursors.length]);

  const goToPreviousPage = useCallback(() => {
    setPageCursors((current) => (current.length > 1 ? current.slice(0, -1) : current));
    paginationAnchorRef.current?.scrollIntoView({ block: "start" });
  }, []);

  const goToNextPage = useCallback(() => {
    const cursor = listQuery.data?.last_id;
    if (!listQuery.data?.has_more || !cursor) return;
    setPageCursors((current) => [...current, cursor]);
    paginationAnchorRef.current?.scrollIntoView({ block: "start" });
  }, [listQuery.data]);

  return (
    <section ref={paginationAnchorRef} className="scroll-mt-16" aria-label="Archived sessions">
      <div className="flex min-h-11 flex-wrap items-center gap-2 border-b px-2 py-1.5">
        <h1 className="mr-auto text-sm font-semibold">Archived sessions</h1>
        {archived.length > 0 &&
          (selectionMode ? (
            <>
              <Button type="button" variant="ghost" size="sm" onClick={selectAll}>
                <CheckIcon className="size-3.5" /> Select visible
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                data-testid="archived-exit-selection"
                onClick={exitSelectionMode}
              >
                Done
              </Button>
            </>
          ) : (
            <Button
              type="button"
              variant="outline"
              size="sm"
              data-testid="archived-toggle-selection"
              onClick={() => setSelectionMode(true)}
            >
              Select
            </Button>
          ))}
      </div>

      <ArchiveLibraryToolbar
        value={view}
        projectOptions={projectOptions}
        hostOptions={hostOptions}
        agentOptions={agentOptions}
        onChange={updateView}
      />

      <div className="flex min-h-10 flex-wrap items-center gap-2 border-b px-2 py-1.5 text-xs text-muted-foreground">
        <label htmlFor="archived-retention">Mark as expired after</label>
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
            className="h-8 w-32 text-xs"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper" align="start">
            {RETENTION_OPTIONS.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {ownedExpiredSessions.length > 0 && (
          <>
            <span className="ml-auto flex items-center gap-1 text-destructive">
              <ClockIcon className="size-3.5" />
              {ownedExpiredSessions.length} expired on this page
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
          </>
        )}
      </div>

      <Dialog open={deleteExpiredOpen} onOpenChange={setDeleteExpiredOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete expired sessions?</DialogTitle>
            <DialogDescription>
              {ownedExpiredSessions.length === 1
                ? "1 owned archived session"
                : ownedExpiredSessions.length + " owned archived sessions"}{" "}
              older than {retentionDays} days on this page will be permanently deleted. This cannot
              be undone.
              {listQuery.data?.has_more && (
                <span className="mt-2 block text-sm">
                  More matching archived sessions exist on later pages; review those pages
                  separately.
                </span>
              )}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              onClick={() => setDeleteExpiredOpen(false)}
              disabled={bulkDelete.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={bulkDelete.isPending}
              onClick={() => {
                bulkDelete.mutate({
                  ids: ownedExpiredSessions.map((conversation) => conversation.id),
                });
                setDeleteExpiredOpen(false);
              }}
            >
              Delete{" "}
              {ownedExpiredSessions.length === 1
                ? "1 session"
                : ownedExpiredSessions.length + " sessions"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {selectionMode && (
        <div className="px-2 pt-2">
          <ArchivedBulkActionBar
            selectedIds={selectedIds}
            allArchived={archived}
            onDeselectAll={deselectAll}
          />
        </div>
      )}

      {listQuery.isLoading ? (
        <p className="px-3 py-4 text-ui text-muted-foreground">Loading…</p>
      ) : archived.length === 0 ? (
        <p className="px-3 py-4 text-ui text-muted-foreground">No archived sessions match.</p>
      ) : (
        <>
          <ul className="flex flex-col p-1">
            {archived.map((conv) => (
              <ArchivedRow
                key={conv.id}
                conversation={conv}
                projectName={
                  (conv.project_id ? projectNames.get(conv.project_id) : undefined) ??
                  conv.labels?.omni_project
                }
                hostName={hostNames.get(conv.host_id ?? "")}
                cutoff={retentionCutoff}
                selectionMode={selectionMode}
                isSelected={selectedIds.has(conv.id)}
                onToggleSelected={toggleSelected}
                isActive={selectedConversationId === conv.id}
                onOpen={onSelectConversation}
              />
            ))}
          </ul>
          {(pageNumber > 1 || listQuery.data?.has_more) && (
            <nav
              className="flex items-center justify-center gap-3 border-t p-2"
              aria-label="Archived session pages"
            >
              <Button
                type="button"
                variant="outline"
                size="sm"
                data-testid="archived-page-previous"
                disabled={pageNumber === 1 || listQuery.isFetching}
                onClick={goToPreviousPage}
              >
                Previous
              </Button>
              <span
                className="min-w-14 text-center text-sm text-muted-foreground"
                aria-live="polite"
              >
                Page {pageNumber}
              </span>
              <Button
                type="button"
                variant="outline"
                size="sm"
                data-testid="archived-page-next"
                disabled={!listQuery.data?.has_more || listQuery.isFetching}
                onClick={goToNextPage}
              >
                Next
              </Button>
            </nav>
          )}
        </>
      )}
    </section>
  );
}

function ArchivedBulkActionBar({
  selectedIds,
  allArchived,
  onDeselectAll,
}: {
  selectedIds: Set<string>;
  allArchived: Conversation[];
  onDeselectAll: () => void;
}) {
  const bulkArchive = useBulkArchiveConversations();
  const bulkDelete = useBulkDeleteConversations();
  const viewerId = useViewerId();
  const ownedSelected = useMemo(
    () =>
      allArchived.filter((conversation) => {
        if (!selectedIds.has(conversation.id)) return false;
        const owner = conversation.owner ?? null;
        return owner === null || owner === viewerId;
      }),
    [allArchived, selectedIds, viewerId],
  );
  const isBusy = bulkArchive.isPending || bulkDelete.isPending;
  const [confirmDeleteOpen, setConfirmDeleteOpen] = useState(false);

  return (
    <>
      <div className="mb-3 flex min-h-10 flex-wrap items-center gap-1.5 rounded-md border bg-muted/50 px-2 py-1.5">
        <span className="mr-auto text-sm text-muted-foreground">
          {selectedIds.size === 0 ? "None selected" : `${selectedIds.size} selected`}
        </span>
        <Button
          type="button"
          variant="outline"
          size="icon-sm"
          disabled={isBusy || ownedSelected.length === 0}
          data-testid="archived-bulk-unarchive"
          aria-label={`Unarchive ${ownedSelected.length} selected session${ownedSelected.length === 1 ? "" : "s"}`}
          onClick={() =>
            bulkArchive.mutate(
              { ids: ownedSelected.map((conversation) => conversation.id), archived: false },
              { onSuccess: onDeselectAll },
            )
          }
        >
          {bulkArchive.isPending ? (
            <Loader2Icon className="size-3 animate-spin" />
          ) : (
            <ArchiveRestoreIcon />
          )}
        </Button>
        <Button
          type="button"
          variant="outline"
          size="icon-sm"
          className="text-destructive"
          disabled={isBusy || ownedSelected.length === 0}
          data-testid="archived-bulk-delete"
          aria-label="Delete selected sessions"
          onClick={() => setConfirmDeleteOpen(true)}
        >
          {bulkDelete.isPending ? <Loader2Icon className="size-3 animate-spin" /> : <Trash2Icon />}
        </Button>
        {(bulkArchive.isError || bulkDelete.isError) && (
          <p className="basis-full text-xs text-destructive" role="alert">
            Some actions failed.
          </p>
        )}
      </div>
      <Dialog open={confirmDeleteOpen} onOpenChange={setConfirmDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete {ownedSelected.length} session(s)?</DialogTitle>
            <DialogDescription>
              This permanently removes the selected session history. This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => setConfirmDeleteOpen(false)}>
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={bulkDelete.isPending}
              onClick={() => {
                setConfirmDeleteOpen(false);
                bulkDelete.mutate(
                  { ids: ownedSelected.map((conversation) => conversation.id) },
                  { onSuccess: onDeselectAll },
                );
              }}
            >
              Confirm
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

/**
 * Bulk action bar for the archived-sessions settings section. Modeled on the
 * sidebar's BulkActionBar but scoped to archived rows — offers Delete and
 * Unarchive, plus Select all / Deselect all / exit controls.
 */
function ArchivedRow({
  conversation,
  projectName,
  hostName,
  cutoff,
  selectionMode,
  isSelected,
  onToggleSelected,
  isActive,
  onOpen,
}: {
  conversation: Conversation;
  projectName?: string;
  hostName?: string;
  cutoff: number | null;
  selectionMode: boolean;
  isSelected: boolean;
  onToggleSelected: (id: string) => void;
  isActive: boolean;
  onOpen: (conversation: Conversation) => void;
}) {
  const navigate = useNavigate();
  const archive = useArchiveConversation();
  const del = useStopAndDeleteConversation();
  const [deleteOpen, setDeleteOpen] = useState(false);
  const label = conversationDisplayLabel(conversation);
  const busy = archive.isPending || del.isPending;
  const archivedAtMs = archivedTimestamp(conversation) * 1000;
  const isExpired = cutoff !== null && archivedAtMs < cutoff * 1000;
  const resolvedHostName = hostName ?? conversation.host_id ?? "Host not recorded";
  const resolvedAgentName = conversation.agent_name ?? "Agent not recorded";
  const matchCount = conversation.search_match_count ?? (conversation.search_match ? 1 : 0);

  return (
    <li
      data-testid="archived-row"
      data-active={isActive || undefined}
      className={cn(
        "group relative flex items-center gap-2 rounded-md border border-transparent hover:border-border hover:bg-muted/60 max-md:flex-wrap max-md:border-border/60 max-md:bg-muted/20",
        isActive && !selectionMode && "border-primary/30 bg-primary/5",
        isSelected && "bg-muted",
      )}
      onClick={(event) => {
        if (selectionMode && event.target === event.currentTarget) {
          onToggleSelected(conversation.id);
        }
      }}
    >
      <button
        type="button"
        data-testid="archived-open-session"
        aria-selected={isActive && !selectionMode}
        aria-pressed={selectionMode ? isSelected : undefined}
        className="flex min-w-0 flex-1 items-center gap-2 rounded-md px-2.5 py-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring"
        onClick={() => (selectionMode ? onToggleSelected(conversation.id) : onOpen(conversation))}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !selectionMode) {
            event.preventDefault();
            onOpen(conversation);
            window.setTimeout(
              () =>
                document.querySelector<HTMLElement>('[data-testid="archive-transcript"]')?.focus(),
              0,
            );
            return;
          }
          if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
          event.preventDefault();
          const rows = [
            ...(event.currentTarget
              .closest("ul")
              ?.querySelectorAll<HTMLButtonElement>('[data-testid="archived-open-session"]') ?? []),
          ];
          const index = rows.indexOf(event.currentTarget);
          rows[index + (event.key === "ArrowDown" ? 1 : -1)]?.focus();
        }}
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
          <div className="flex min-w-0 items-center gap-1.5">
            <span className="truncate text-sm font-medium" title={label}>
              {label}
            </span>
            {projectName && (
              <span className="max-w-28 shrink-0 truncate rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                {projectName}
              </span>
            )}
            {isExpired && (
              <span className="shrink-0 rounded bg-destructive/10 px-1.5 py-0.5 text-[10px] font-medium text-destructive">
                Expired
              </span>
            )}
          </div>
          <div
            className="mt-0.5 flex min-w-0 items-center gap-1.5 truncate text-[11px] text-muted-foreground"
            data-testid="archived-context"
          >
            <span className="truncate" title={resolvedHostName}>
              {resolvedHostName}
            </span>
            <span aria-hidden="true">·</span>
            <span className="truncate" title={resolvedAgentName}>
              {resolvedAgentName}
            </span>
            <span aria-hidden="true">·</span>
            <time
              className="shrink-0"
              dateTime={new Date(archivedAtMs).toISOString()}
              title={`Archived ${new Date(archivedAtMs).toLocaleString("en-US")}`}
            >
              {new Date(archivedAtMs).toLocaleDateString("en-US", {
                month: "short",
                day: "numeric",
              })}
            </time>
            {conversation.search_match && (
              <span className="shrink-0 text-info">· {matchCount} content match</span>
            )}
          </div>
        </div>
      </button>
      {!selectionMode && (
        <div className="flex shrink-0 items-center gap-1 max-md:mr-14 max-md:ml-auto">
          <Tooltip>
            <TooltipTrigger asChild>
              <span>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  className="max-md:size-11"
                  aria-label="Delete session"
                  data-testid="delete-archived"
                  disabled={busy}
                  onClick={() => setDeleteOpen(true)}
                >
                  <Trash2Icon className="size-4 text-destructive" />
                </Button>
              </span>
            </TooltipTrigger>
            <TooltipContent>Delete permanently</TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                className="dark:bg-secondary dark:hover:bg-secondary/80 max-md:size-11"
                aria-label="Unarchive session"
                data-testid="unarchive-conversation"
                disabled={busy}
                onClick={() =>
                  archive.mutate(
                    { id: conversation.id, archived: false },
                    { onSuccess: () => navigate(`/c/${conversation.id}`) },
                  )
                }
              >
                <ArchiveRestoreIcon className="size-4" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>Unarchive</TooltipContent>
          </Tooltip>
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
