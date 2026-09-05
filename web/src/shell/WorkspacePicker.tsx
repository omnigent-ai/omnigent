import {
  FolderDotIcon,
  FolderIcon,
  FolderPlusIcon,
  FileIcon,
  ArrowUpIcon,
  HomeIcon,
  EyeIcon,
  EyeOffIcon,
  CheckIcon,
  XIcon,
  AlertTriangleIcon,
  HardDriveIcon,
  PinIcon,
  SearchIcon,
} from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import {
  useCreateHostDirectory,
  useHostFilesystem,
  useHostFilesystemRoots,
} from "@/hooks/useHostFilesystem";
import { setHostDefaultWorkspace, useHosts } from "@/hooks/useHosts";

const WINDOWS_DRIVE_ROOT_RE = /^[A-Za-z]:[\\/]$/;
const WINDOWS_ABSOLUTE_RE = /^[A-Za-z]:[\\/]/;
const UNC_ABSOLUTE_RE = /^\\\\[^\\/]+[\\/][^\\/]+/;

/** True for POSIX, drive-letter, or UNC absolute paths. */
export function isAbsoluteHostPath(path: string): boolean {
  return path.startsWith("/") || WINDOWS_ABSOLUTE_RE.test(path) || UNC_ABSOLUTE_RE.test(path);
}

function isWindowsPath(path: string): boolean {
  return WINDOWS_ABSOLUTE_RE.test(path) || UNC_ABSOLUTE_RE.test(path);
}

/**
 * Join a directory path and a new child name into an absolute path.
 *
 * Handles the filesystem root (``"/"`` + ``"foo"`` → ``"/foo"`` rather
 * than ``"//foo"``) and trims a trailing slash off the parent so a
 * typed ``"/Users/me/"`` still produces ``"/Users/me/foo"``. The child
 * name is trimmed; surrounding/duplicate slashes in it are left to the
 * host to resolve.
 *
 * @param dir Absolute parent directory, e.g. ``"/Users/me"`` or ``"/"``.
 * @param name New child name, e.g. ``"new-app"``.
 * @returns The joined absolute path, e.g. ``"/Users/me/new-app"``.
 */
export function joinPath(dir: string, name: string): string {
  const trimmedName = name.trim();
  if (isWindowsPath(dir)) {
    const base = dir.replace(/[\\/]+$/, "");
    return `${base}\\${trimmedName}`;
  }
  if (dir === "/") {
    return `/${trimmedName}`;
  }
  const base = dir.endsWith("/") ? dir.slice(0, -1) : dir;
  return `${base}/${trimmedName}`;
}

/**
 * Compute the parent directory of an absolute path.
 *
 * Returns ``null`` when the input is empty (host's home view —
 * has no parent in the picker's UX) or already at the root
 * ``"/"``. Otherwise drops the last segment.
 *
 * @param absolutePath Absolute path or empty string.
 * @returns Parent path, or ``null`` if there is no further parent.
 */
export function parentOf(absolutePath: string): string | null {
  if (absolutePath === "" || absolutePath === "/") {
    return null;
  }
  if (isWindowsPath(absolutePath)) {
    const canonical = absolutePath.replace(/\//g, "\\");
    if (WINDOWS_DRIVE_ROOT_RE.test(canonical)) return null;
    const stripped = canonical.replace(/\\+$/, "");
    const idx = stripped.lastIndexOf("\\");
    if (idx === 2 && /^[A-Za-z]:/.test(stripped)) return `${stripped.slice(0, 2)}\\`;
    if (idx < 0) return null;
    const parent = stripped.slice(0, idx);
    // A UNC share is itself a root in this picker.
    if (/^\\\\[^\\]+\\[^\\]+$/.test(stripped)) return null;
    return parent;
  }
  const stripped = absolutePath.endsWith("/") ? absolutePath.slice(0, -1) : absolutePath;
  const idx = stripped.lastIndexOf("/");
  if (idx <= 0) {
    return "/";
  }
  return stripped.slice(0, idx);
}

/**
 * Normalize a path the user typed into the path input.
 *
 * Trims whitespace, expands a leading ``~`` against the resolved
 * home directory, collapses runs of slashes, and drops a trailing
 * slash (except on the root ``"/"``). Returns ``null`` for empty
 * or invalid inputs (which the caller treats as "ignore — keep
 * the current path"). The picker never turns a typed path into
 * the empty string; "go home" is its own gesture (clicking the
 * Home breadcrumb).
 *
 * Tilde-only (``"~"``) and ``"~/foo"`` are expanded to
 * ``home`` and ``home + "/foo"`` respectively. If ``home`` is
 * ``null`` (we haven't resolved it yet from the first listing),
 * tilde input is rejected so the user isn't sent to the wrong
 * place. Bare ``~user`` form is not supported.
 *
 * @param input Whatever the user typed, e.g.
 *   ``"  /Users//corey/  "`` or ``"~/projects"``.
 * @param home Resolved absolute path of the host's home dir, or
 *   ``null`` if not yet known.
 * @returns Cleaned absolute path (e.g. ``"/Users/corey"``) or
 *   ``null`` when the input isn't usable.
 */
export function normalizeTypedPath(input: string, home: string | null = null): string | null {
  const trimmed = input.trim();
  if (trimmed === "") {
    return null;
  }
  let absolute: string;
  if (trimmed === "~") {
    // Bare tilde — go home if we know where that is.
    if (home === null) return null;
    absolute = home;
  } else if (trimmed.startsWith("~/") || trimmed.startsWith("~\\")) {
    // ~/foo or ~\foo → <home>/<foo> in the home path's native style.
    // Reject when home isn't resolved yet.
    if (home === null) return null;
    absolute = joinPath(home, trimmed.slice(2));
  } else if (trimmed.startsWith("/") || isWindowsPath(trimmed)) {
    absolute = trimmed;
  } else {
    // Relative paths and ~user forms are not supported — the host
    // endpoint requires absolute paths.
    return null;
  }
  if (isWindowsPath(absolute)) {
    const canonical = absolute.replace(/\//g, "\\");
    if (canonical.startsWith("\\\\")) {
      const tail = canonical.slice(2).replace(/\\+/g, "\\").replace(/\\$/, "");
      return `\\\\${tail}`;
    }
    const driveCanonical = `${canonical[0].toUpperCase()}${canonical.slice(1)}`;
    if (WINDOWS_DRIVE_ROOT_RE.test(driveCanonical)) return driveCanonical;
    return driveCanonical.replace(/\\+/g, "\\").replace(/\\$/, "");
  }
  // Collapse runs of slashes ("//" → "/") so a typo doesn't
  // produce a path the host can't list.
  const collapsed = absolute.replace(/\/+/g, "/");
  if (collapsed === "/") {
    return "/";
  }
  // Drop trailing slash so parent calc stays stable.
  return collapsed.endsWith("/") ? collapsed.slice(0, -1) : collapsed;
}

/**
 * Basename of an absolute path, for the "Select current" label.
 *
 * @param absolutePath Current directory, e.g.
 *   ``"/Users/corey/projects"``, ``"/"``, or ``""`` (home,
 *   pre-resolution).
 * @returns The last path segment (``"projects"``), ``"/"`` for the
 *   root, or ``"~"`` when the path is still the empty placeholder.
 */
export function basename(absolutePath: string): string {
  if (absolutePath === "") {
    return "~";
  }
  if (absolutePath === "/") {
    return "/";
  }
  if (WINDOWS_DRIVE_ROOT_RE.test(absolutePath)) return absolutePath.replace("/", "\\");
  const windowsPath = isWindowsPath(absolutePath);
  const stripped =
    absolutePath.endsWith("/") || (windowsPath && absolutePath.endsWith("\\"))
      ? absolutePath.slice(0, -1)
      : absolutePath;
  const separatorIndex = windowsPath
    ? Math.max(stripped.lastIndexOf("/"), stripped.lastIndexOf("\\"))
    : stripped.lastIndexOf("/");
  return stripped.slice(separatorIndex + 1);
}

/**
 * True when a path can be opened in the picker: an absolute path or a
 * home-relative one (``~`` / ``~/foo``). The host expands ``~`` server
 * side, so these navigate fine; relative paths and the ``~user`` form
 * do not and are rejected.
 *
 * @param path Raw path text, e.g. ``"~/projects"`` or ``"/tmp"``.
 * @returns Whether the picker can navigate to it.
 */
export function isNavigablePath(path: string): boolean {
  const trimmed = path.trim();
  return (
    isAbsoluteHostPath(trimmed) ||
    trimmed === "~" ||
    trimmed.startsWith("~/") ||
    trimmed.startsWith("~\\")
  );
}

/**
 * Icon button in the picker header, with a styled hover tooltip.
 *
 * The tooltip hangs off a wrapping span rather than the button so it still
 * appears while the button is *disabled* — that is exactly when a user is
 * most likely to hover asking "what is this, and why can't I click it?".
 * A disabled button receives no pointer events of its own.
 *
 * @param label Tooltip text, also the accessible name.
 * @param icon Rendered glyph.
 * @param onClick Activation handler.
 * @param disabled Whether the action is unavailable.
 * @param testId ``data-testid`` for the button.
 */
function PickerIconButton({
  label,
  icon,
  onClick,
  disabled = false,
  testId,
}: {
  label: string;
  icon: ReactNode;
  onClick: () => void;
  disabled?: boolean;
  testId: string;
}) {
  return (
    // Provides its own context so the button works wherever it is rendered --
    // the picker is mounted in dialogs and popovers, and in tests, not only
    // under the app-root provider. Mirrors FilesPanel's hidden-files toggle.
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger
          asChild
          // Opening the picker focuses its first button, and Radix pops a
          // tooltip on any focus — a label thrown over the listing nobody asked
          // for. Only a keyboard focus ring reveals it (Radix skips its own
          // handler once the event's default is prevented).
          onFocus={(event) => {
            if (!(event.target as HTMLElement).matches(":focus-visible")) {
              event.preventDefault();
            }
          }}
        >
          <span className="shrink-0">
            <button
              type="button"
              onClick={onClick}
              disabled={disabled}
              aria-label={label}
              className="block rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground disabled:opacity-30"
              data-testid={testId}
            >
              {icon}
            </button>
          </span>
        </TooltipTrigger>
        <TooltipContent side="bottom">{label}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export interface WorkspacePickerProps {
  /** Host to browse, or ``null`` to render an empty state. */
  hostId: string | null;
  /**
   * Called with the current directory's absolute path when the user
   * clicks "Select current". ``undefined`` hides that button.
   */
  onSelect?: (path: string) => void;
  /**
   * Called with the current directory's absolute path whenever the user
   * navigates (clicks a folder, goes up/home, commits a typed path), so a
   * caller can track the selection live without an explicit "Select" click.
   * Distinct from ``onSelect`` (which is a one-shot commit + the button):
   * pass ``onNavigate`` for a live-updating picker with no button.
   */
  onNavigate?: (path: string) => void;
  /**
   * Called when the user dismisses the picker via the ✕ button.
   * ``undefined`` hides the button (e.g. when the picker is always
   * shown rather than toggled).
   */
  onClose?: () => void;
  /**
   * Absolute path to open the picker at on mount, e.g.
   * ``"/Users/corey/projects"``. ``undefined`` starts at the Host's home
   * directory. Read only at
   * mount time; later changes are ignored (navigate via the picker UI instead).
   */
  initialPath?: string;
  /**
   * How many other live agents are working in a given absolute directory,
   * used to show a conflict banner for the directory currently being browsed.
   * Called per render with the picker's current absolute path (e.g.
   * ``"/Users/corey/repo"``); return ``0`` for no conflict. ``undefined``
   * disables the banner entirely.
   */
  occupancyForPath?: (absolutePath: string) => number;
  /**
   * Absolute path of the session's workspace, e.g.
   * ``"/Users/corey/repo"``. When set, a "back to workspace" button appears
   * beside Home so a user who has wandered off can return in one click.
   * Omit where there is no workspace yet — the new-session / fork / project
   * dialogs are *choosing* one, so for them Home (``~``) is the only
   * meaningful anchor.
   */
  workspacePath?: string;
  /** Host-level folder pinned as a picker shortcut. */
  defaultPath?: string | null;
  /** Human-readable Host name used to make the pin action's scope explicit. */
  defaultPathHostName?: string;
  /** Set or clear the host-level pinned folder. */
  onDefaultPathChange?: (path: string | null) => void | Promise<void>;
  /** Whether this Host advertises platform-root enumeration support. */
  supportsFilesystemRoots?: boolean;
}

export type HostWorkspacePickerProps = Omit<
  WorkspacePickerProps,
  "defaultPath" | "defaultPathHostName" | "onDefaultPathChange"
>;

/**
 * Host-aware workspace browser used by every product entry point.
 *
 * Keeping the Host preference lookup and mutation here prevents new-session,
 * project settings, fork/resume/switch, scheduled-task, and an existing
 * conversation's Working folder from drifting into different browsers. The
 * lower-level {@link WorkspacePicker} stays injectable for stories and focused
 * rendering tests.
 */
export function HostWorkspacePicker(props: HostWorkspacePickerProps) {
  const queryClient = useQueryClient();
  const { data: hosts } = useHosts({ enabled: props.hostId !== null });
  const host = hosts?.find((candidate) => candidate.host_id === props.hostId);

  return (
    <WorkspacePicker
      {...props}
      defaultPath={host?.default_workspace}
      defaultPathHostName={host?.name ?? props.hostId ?? undefined}
      supportsFilesystemRoots={host?.filesystem_roots === true}
      onDefaultPathChange={
        props.hostId === null
          ? undefined
          : async (path) => {
              await setHostDefaultWorkspace(props.hostId as string, path);
              await queryClient.invalidateQueries({ queryKey: ["hosts"] });
            }
      }
    />
  );
}

/**
 * Flat-list directory picker for choosing a workspace.
 *
 * Two compact header rows sit above the current directory's entries:
 * navigation and actions first, then exact-path input, optional Host pin,
 * and current-level folder search. Clicking a folder navigates into it.
 * The "Select" button stays in the always-visible action row so it doesn't
 * fall below the fold on short screens. Files are grayed out because
 * workspaces must be directories.
 *
 * @param hostId Host whose filesystem to browse.
 * @param onSelect Fired with the current directory on "Select
 *   current". Omit to hide that button.
 * @param onClose Fired when the ✕ button is clicked.
 * @param onNavigate Fired with the current directory on every navigation,
 *   for a live-updating picker with no "Select" button.
 * @param initialPath Absolute path to open at on mount; defaults to the
 *   Host's home directory. The pinned folder remains an explicit shortcut.
 * @param occupancyForPath Returns how many other live agents occupy a given
 *   absolute directory; drives the conflict banner. Omit to disable it.
 */
export function WorkspacePicker({
  hostId,
  onSelect,
  onClose,
  onNavigate,
  initialPath,
  occupancyForPath,
  workspacePath,
  defaultPath,
  defaultPathHostName,
  onDefaultPathChange,
  supportsFilesystemRoots = false,
}: WorkspacePickerProps) {
  // "" means home — the server forwards ~ to list_dir. initialPath
  // seeds the start dir (read once at mount).
  const [path, setPath] = useState<string>(initialPath ?? "");
  const [showRoots, setShowRoots] = useState(false);
  // The editable path value; diverges from `path` while typing and
  // snaps back on commit (Enter / blur).
  const [pathInput, setPathInput] = useState<string>("");
  // Resolved absolute home, derived lazily from the first listing so
  // "Select current" returns a real path even at the home view.
  const [resolvedHome, setResolvedHome] = useState<string | null>(null);
  // Dot-prefixed entries (.git / .venv) are hidden until toggled on.
  const [showHidden, setShowHidden] = useState(false);
  // True while the user is editing the path bar, so a late listing
  // (e.g. home resolving) can't overwrite what they're typing.
  const userEditedRef = useRef(false);
  // "New folder" inline form: null when closed, otherwise the in-progress
  // folder name. A separate error string holds the last create failure
  // (e.g. "directory already exists") so it shows inline by the input.
  const [newFolderName, setNewFolderName] = useState<string | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const [defaultError, setDefaultError] = useState<string | null>(null);
  // Folder-name search is separate from the address bar: the latter always
  // means "open this exact path", while this field filters the current level.
  const [searchQuery, setSearchQuery] = useState("");
  const createDir = useCreateHostDirectory();

  // Reset to home when the host *changes* — a path from the old host
  // is meaningless on the new one. Compare the previous hostId rather
  // than a "first run" flag: the latter resets on mount under
  // StrictMode's double-invoke and clobbers the ``initialPath`` seed.
  const prevHostId = useRef(hostId);
  useEffect(() => {
    const hostChanged = prevHostId.current !== hostId;
    prevHostId.current = hostId;
    if (!hostChanged) return;

    // initialPath is mount-only. During a Host switch, callers can briefly
    // retain the old Host's workspace; never carry that path across Hosts.
    setPath("");
    setShowRoots(false);
    setPathInput("");
    setResolvedHome(null);
    userEditedRef.current = false;
    setNewFolderName(null);
    setCreateError(null);
    setDefaultError(null);
    setSearchQuery("");
  }, [hostId]);

  const directoryQuery = useHostFilesystem(hostId, showRoots ? null : path);
  const rootsQuery = useHostFilesystemRoots(hostId, supportsFilesystemRoots && showRoots);
  const { data, isLoading, error, isPlaceholderData } = showRoots ? rootsQuery : directoryQuery;

  // Resolve the host's home dir independently of where the picker is
  // browsing, so a typed "~"-relative path can be expanded even when the
  // picker opened straight at an absolute initialPath and thus never visits
  // the "" home view. The query fires at mount and disables once home
  // resolves; when the picker IS at the home view it shares the main
  // listing's query key, so this adds no extra fetch there. An empty home
  // has no entry to derive from and stays unresolved (the picker still
  // opens onto it fine, and "~" typing is moot in an empty home).
  const { data: homeData, isPlaceholderData: homeIsPlaceholder } = useHostFilesystem(
    hostId,
    resolvedHome === null ? "" : null,
  );

  // Derive the home dir's absolute path from the first entry's parent (all
  // entries share one parent). Skip placeholder data (the prior directory
  // kept on screen during a load) or we'd derive home from the wrong dir.
  useEffect(() => {
    if (resolvedHome !== null || homeIsPlaceholder || !homeData || homeData.entries.length === 0) {
      return;
    }
    const first = homeData.entries[0];
    // first.path is "/Users/corey/x" → parent is "/Users/corey".
    setResolvedHome(parentOf(first.path));
  }, [resolvedHome, homeData, homeIsPlaceholder]);

  // Absolute path of the directory currently shown, derived from the
  // first entry's parent (entries share one parent). This is how a ""
  // (home) or "~"-relative path — both expanded by the host — gets
  // resolved back to an absolute path. null while loading or for an
  // empty / placeholder listing.
  const listedAbsolute =
    !isPlaceholderData && data && data.entries.length > 0 ? parentOf(data.entries[0].path) : null;

  // The absolute path the picker currently represents — used for
  // breadcrumbs and the selection callback. An absolute path is taken
  // as-is; "" (home) or a "~"-relative path uses the absolute the host
  // resolved it to, falling back to the raw path until the listing
  // arrives (so the breadcrumb stays put rather than flashing empty).
  const currentAbsolute = showRoots
    ? ""
    : isAbsoluteHostPath(path)
      ? path
      : (listedAbsolute ?? path);

  // Other live agents working in the directory currently shown. Only a
  // resolved absolute path can match a stored workspace; the home view ("")
  // and unresolved paths report no conflict.
  const occupiedCount =
    occupancyForPath && isAbsoluteHostPath(currentAbsolute) ? occupancyForPath(currentAbsolute) : 0;

  // Mirror navigation into the path input so it reflects where the
  // listing came from (the user can still overwrite it). Skip while
  // the user is typing so a late home-resolve doesn't clobber them.
  useEffect(() => {
    if (userEditedRef.current) return;
    setPathInput(currentAbsolute);
  }, [currentAbsolute]);

  // Report the current directory to the caller as the user navigates, so a
  // live-updating caller (no "Select" button) tracks the selection. Held in a
  // ref so an inline callback prop doesn't refire the effect every render —
  // it fires only when currentAbsolute actually changes.
  const onNavigateRef = useRef(onNavigate);
  onNavigateRef.current = onNavigate;
  useEffect(() => {
    if (isAbsoluteHostPath(currentAbsolute)) {
      onNavigateRef.current?.(currentAbsolute);
    }
  }, [currentAbsolute]);

  const parent = parentOf(currentAbsolute);

  const normalizedSearch = searchQuery.trim().toLowerCase();
  // Searching a dot-prefixed name reveals hidden entries even with the
  // toggle off, so ".env" can be found without flipping "Show hidden".
  const includeHidden = showHidden || normalizedSearch.startsWith(".");

  // Directories first, then files, alphabetical. Dot-prefixed entries
  // are hidden unless "Show hidden" is on. Search is intentionally limited
  // to folders in the current directory; the Host API does not recursively
  // index the machine, so the UI must not imply a full-disk search.
  const entries = (data?.entries ?? [])
    .filter((e) => includeHidden || !e.name.startsWith("."))
    .filter(
      (e) =>
        normalizedSearch === "" ||
        (e.type === "directory" && e.name.toLowerCase().includes(normalizedSearch)),
    )
    .sort((a, b) => {
      if (a.type === "directory" && b.type !== "directory") return -1;
      if (a.type !== "directory" && b.type === "directory") return 1;
      return a.name.localeCompare(b.name);
    });

  function navigateTo(next: string) {
    // A click/commit supersedes any in-progress typing; let the
    // mirror effect refill the bar from the new listing.
    userEditedRef.current = false;
    setShowRoots(false);
    setPath(next);
    setSearchQuery("");
  }

  function navigateToRoots() {
    userEditedRef.current = false;
    setShowRoots(true);
    setPathInput("");
    setSearchQuery("");
  }

  function commitPathInput() {
    const normalized = normalizeTypedPath(pathInput, resolvedHome);
    userEditedRef.current = false;
    if (normalized === null) {
      // Unusable input — snap back so the user can keep editing.
      setPathInput(currentAbsolute);
      return;
    }
    if (normalized !== currentAbsolute) {
      navigateTo(normalized);
    } else {
      // Same directory — snap the text back to the canonical form.
      setPathInput(currentAbsolute);
    }
  }

  function handleSelect() {
    if (currentAbsolute === "" || currentAbsolute === null) {
      return;
    }
    onSelect?.(currentAbsolute);
  }

  // Directory the "New folder" action creates in. A resolved absolute
  // path is used as-is. At the home view the absolute path is derived
  // from the first listing entry, so an *empty* home yields no entry and
  // never resolves — fall back to "~" (the host expands it) once the
  // listing has loaded, otherwise creating the first folder in an empty
  // home would be impossible. Stays null while loading so the button is
  // disabled until we know what home resolves to.
  const createBaseDir = isAbsoluteHostPath(currentAbsolute)
    ? currentAbsolute
    : path === "" && !isLoading && !isPlaceholderData
      ? "~"
      : null;
  const canCreateFolder = hostId !== null && createBaseDir !== null;

  function openNewFolder() {
    setCreateError(null);
    setNewFolderName("");
  }

  function cancelNewFolder() {
    setNewFolderName(null);
    setCreateError(null);
  }

  async function commitNewFolder() {
    const name = (newFolderName ?? "").trim();
    if (name === "" || hostId === null || createBaseDir === null) {
      return;
    }
    const target = joinPath(createBaseDir, name);
    try {
      const created = await createDir.mutateAsync({ hostId, path: target });
      // Drop into the freshly created folder so the user can pick it
      // straight away (the reason they made it). The listing refresh is
      // handled by the mutation's onSuccess invalidation.
      setNewFolderName(null);
      setCreateError(null);
      navigateTo(created);
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : "Failed to create folder");
    }
  }

  async function toggleDefaultPath() {
    if (!onDefaultPathChange || !isAbsoluteHostPath(currentAbsolute)) return;
    setDefaultError(null);
    try {
      await onDefaultPathChange(currentAbsolute === defaultPath ? null : currentAbsolute);
    } catch (err) {
      setDefaultError(err instanceof Error ? err.message : "Failed to save the pinned folder");
    }
  }

  return (
    <div
      className="flex max-h-80 min-h-0 flex-col rounded-md border"
      data-testid="workspace-picker"
    >
      <div className="flex shrink-0 items-center gap-1.5 border-b px-2 py-1.5">
        <PickerIconButton
          label="Up one level"
          icon={<ArrowUpIcon className="size-4" />}
          onClick={() => {
            if (parent !== null) navigateTo(parent);
            else if (supportsFilesystemRoots) navigateToRoots();
          }}
          disabled={
            showRoots || currentAbsolute === "" || (parent === null && !supportsFilesystemRoots)
          }
          testId="workspace-picker-up"
        />
        {supportsFilesystemRoots && (
          <PickerIconButton
            label="Computer roots"
            icon={<HardDriveIcon className="size-4" />}
            onClick={navigateToRoots}
            disabled={showRoots}
            testId="workspace-picker-roots"
          />
        )}
        {workspacePath !== undefined && (
          <PickerIconButton
            label="Workspace root"
            icon={<FolderDotIcon className="size-4" />}
            onClick={() => navigateTo(workspacePath)}
            disabled={currentAbsolute === workspacePath}
            testId="workspace-picker-workspace"
          />
        )}
        <PickerIconButton
          label="Home"
          icon={<HomeIcon className="size-4" />}
          onClick={() => navigateTo("")}
          testId="workspace-picker-home"
        />
        {defaultPath && currentAbsolute !== defaultPath && (
          <PickerIconButton
            label={`Open pinned folder: ${defaultPath}`}
            icon={<PinIcon className="size-4" />}
            onClick={() => navigateTo(defaultPath)}
            testId="workspace-picker-open-pinned"
          />
        )}
        <div className="flex-1" />
        <PickerIconButton
          label={showHidden ? "Hide hidden files" : "Show hidden files"}
          icon={showHidden ? <EyeIcon className="size-4" /> : <EyeOffIcon className="size-4" />}
          onClick={() => setShowHidden((v) => !v)}
          testId="workspace-picker-show-hidden"
        />
        <PickerIconButton
          label="New folder"
          icon={<FolderPlusIcon className="size-4" />}
          onClick={openNewFolder}
          disabled={!canCreateFolder}
          testId="workspace-picker-new-folder"
        />
        {onSelect && (
          <Button
            type="button"
            size="sm"
            disabled={showRoots || currentAbsolute === "" || currentAbsolute === null}
            onClick={handleSelect}
            title={`Select this folder: ${basename(currentAbsolute)}`}
            className="shrink-0"
            data-testid="workspace-picker-select"
          >
            <CheckIcon className="size-3.5" />
            Select
          </Button>
        )}
        {onClose && (
          <PickerIconButton
            label="Close"
            icon={<XIcon className="size-4" />}
            onClick={onClose}
            testId="workspace-picker-close"
          />
        )}
      </div>
      <div className="flex shrink-0 items-center gap-1.5 border-b px-2 py-1.5">
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <input
                type="text"
                value={pathInput}
                onChange={(e) => {
                  userEditedRef.current = true;
                  setPathInput(e.target.value);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    commitPathInput();
                  }
                }}
                onBlur={commitPathInput}
                placeholder={showRoots ? "Computer" : "~"}
                aria-label="Folder path. Type an absolute path and press Enter to open it."
                disabled={showRoots}
                spellCheck={false}
                autoCapitalize="off"
                autoCorrect="off"
                className="min-w-0 flex-1 rounded-md bg-muted/40 px-2 py-1 text-sm text-muted-foreground focus:outline-none"
                data-testid="workspace-picker-path-input"
              />
            </TooltipTrigger>
            <TooltipContent side="bottom">Type an absolute path and press Enter</TooltipContent>
          </Tooltip>
        </TooltipProvider>
        {onDefaultPathChange && (
          <PickerIconButton
            label={
              currentAbsolute !== "" && currentAbsolute === defaultPath
                ? `Unpin this folder for ${defaultPathHostName ?? "this Host"}. Pinning only provides quick access; new sessions remember the last working folder.`
                : `Pin this folder for quick access on ${defaultPathHostName ?? "this Host"}. New sessions remember the last working folder.`
            }
            icon={
              <PinIcon
                className={
                  currentAbsolute !== "" && currentAbsolute === defaultPath
                    ? "size-4 fill-current"
                    : "size-4"
                }
              />
            }
            onClick={() => void toggleDefaultPath()}
            disabled={showRoots || !isAbsoluteHostPath(currentAbsolute)}
            testId="workspace-picker-default"
          />
        )}
        <div className="flex min-w-0 flex-1 items-center gap-1.5 rounded-md bg-muted/40 px-2 py-1">
          <SearchIcon className="size-3.5 shrink-0 text-muted-foreground" />
          <input
            type="text"
            value={searchQuery}
            onChange={(event) => setSearchQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                setSearchQuery("");
              }
            }}
            placeholder={showRoots ? "Open a root to search" : "Search folders here"}
            aria-label="Search folders in this directory"
            disabled={showRoots}
            className="min-w-0 flex-1 bg-transparent text-sm text-foreground focus:outline-none disabled:opacity-50"
            data-testid="workspace-picker-search-input"
          />
          {searchQuery !== "" && (
            <button
              type="button"
              onClick={() => setSearchQuery("")}
              aria-label="Clear folder search"
              className="shrink-0 rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-accent-foreground"
            >
              <XIcon className="size-3.5" />
            </button>
          )}
        </div>
      </div>
      {defaultError !== null && (
        <div className="border-b px-3 py-2 text-sm text-destructive" role="alert">
          {defaultError}
        </div>
      )}
      {newFolderName !== null && (
        <div
          className="flex shrink-0 flex-col gap-1 border-b px-3 py-1.5"
          data-testid="workspace-picker-new-folder-form"
        >
          <div className="flex items-center gap-2">
            <FolderPlusIcon className="size-4 shrink-0 text-muted-foreground" />
            <input
              type="text"
              // Focus belongs on the field the user just opened; the picker is already a focus trap.
              autoFocus
              value={newFolderName}
              onChange={(e) => {
                setNewFolderName(e.target.value);
                if (createError !== null) setCreateError(null);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void commitNewFolder();
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  cancelNewFolder();
                }
              }}
              placeholder="New folder name"
              spellCheck={false}
              autoCapitalize="off"
              autoCorrect="off"
              className="min-w-0 flex-1 bg-transparent text-sm text-foreground focus:outline-none"
              data-testid="workspace-picker-new-folder-input"
            />
            <button
              type="button"
              disabled={newFolderName.trim() === "" || createDir.isPending}
              onClick={() => void commitNewFolder()}
              aria-label="Create folder"
              title="Create folder"
              className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground disabled:opacity-30"
              data-testid="workspace-picker-new-folder-create"
            >
              {createDir.isPending ? (
                <Spinner className="size-4" />
              ) : (
                <CheckIcon className="size-4" />
              )}
            </button>
            <button
              type="button"
              onClick={cancelNewFolder}
              aria-label="Cancel new folder"
              title="Cancel"
              className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground"
              data-testid="workspace-picker-new-folder-cancel"
            >
              <XIcon className="size-4" />
            </button>
          </div>
          {createError !== null && (
            <span
              className="text-sm text-destructive"
              data-testid="workspace-picker-new-folder-error"
            >
              {createError}
            </span>
          )}
        </div>
      )}
      {occupiedCount > 0 && (
        <div
          className="flex shrink-0 items-start gap-1.5 border-b bg-warning/10 px-3 py-2 text-sm text-warning"
          data-testid="workspace-picker-conflict"
        >
          <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
          <span>
            {occupiedCount === 1 ? "1 other agent is" : `${occupiedCount} other agents are`} working
            in this directory. Write operations may conflict — name a git branch to work in an
            isolated copy.
          </span>
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-y-auto">
        {isLoading && <div className="px-3 py-3 text-sm text-muted-foreground">Loading…</div>}
        {error !== null && error !== undefined && !isLoading && (
          <div className="px-3 py-3 text-sm text-destructive" data-testid="workspace-picker-error">
            {error instanceof Error ? error.message : "Failed to load directory"}
          </div>
        )}
        {!isLoading && error === null && entries.length === 0 && (
          <div className="px-3 py-3 text-sm text-muted-foreground">
            {normalizedSearch !== ""
              ? "No matching folders in this directory"
              : "(empty directory)"}
          </div>
        )}
        {entries.map((entry) => {
          const isDir = entry.type === "directory";
          return (
            <button
              key={entry.path}
              type="button"
              disabled={!isDir}
              // Preventing the mouse-down focus shift keeps an edited path from
              // blurring and committing before the folder click can navigate.
              // onClick still fires for pointer and keyboard activation.
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => isDir && navigateTo(entry.path)}
              className={
                "flex w-full items-center gap-2 border-b px-3 py-2 text-left text-sm last:border-b-0 " +
                (isDir
                  ? "hover:bg-accent hover:text-accent-foreground cursor-pointer"
                  : "text-muted-foreground cursor-not-allowed")
              }
              data-testid={`workspace-picker-entry-${entry.name}`}
            >
              {isDir ? <FolderIcon className="size-4" /> : <FileIcon className="size-4" />}
              <span className="flex-1 truncate">{entry.name}</span>
            </button>
          );
        })}
        {data?.truncated && (
          <div
            className="px-3 py-2 text-sm text-muted-foreground"
            data-testid="workspace-picker-truncated"
          >
            Too many entries to list fully — type a path above to jump directly.
          </div>
        )}
      </div>
    </div>
  );
}
