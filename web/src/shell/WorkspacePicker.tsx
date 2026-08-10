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
} from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { useCreateHostDirectory, useHostFilesystem } from "@/hooks/useHostFilesystem";

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
  } else if (trimmed.startsWith("~/")) {
    // ~/foo → <home>/foo. Reject when home isn't resolved yet.
    if (home === null) return null;
    absolute = `${home}/${trimmed.slice(2)}`;
  } else if (trimmed.startsWith("/")) {
    absolute = trimmed;
  } else {
    // Relative paths and ~user forms are not supported — the host
    // endpoint requires absolute paths.
    return null;
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
  const parts = absolutePath.split("/").filter((p) => p.length > 0);
  return parts[parts.length - 1] ?? absolutePath;
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
  return trimmed.startsWith("/") || trimmed === "~" || trimmed.startsWith("~/");
}

/**
 * Live filter for the listing, derived from the path-bar text.
 *
 * Returns the fragment to match the current directory's entries against
 * (case-insensitive prefix), or ``null`` when the text isn't filtering the
 * current directory — it's blank, it's exactly the current path, or it's a
 * path into a *different* directory the user is navigating to (Enter jumps
 * there). Mirrors shell tab-completion: a bare fragment (``"pro"``) or a
 * trailing segment under the current dir (``"/Users/me/pro"``) narrows the
 * list; anything else leaves it whole.
 *
 * @param pathInput Raw path-bar text, e.g. ``"pro"`` or ``"/Users/me/pro"``.
 * @param currentAbsolute Absolute path of the directory currently shown.
 * @param home Resolved home dir (for ``~`` expansion), or ``null``.
 * @returns The fragment to filter by, or ``null`` for no filter.
 */
export function listingFilter(
  pathInput: string,
  currentAbsolute: string,
  home: string | null = null,
): string | null {
  const trimmed = pathInput.trim();
  if (trimmed === "") return null;
  const slash = trimmed.lastIndexOf("/");
  if (slash === -1) {
    // Bare fragment, no directory part → filter the current dir by it.
    return trimmed;
  }
  const partial = trimmed.slice(slash + 1);
  if (partial === "") return null; // "<dir>/" — nothing typed past the slash.
  // A fragment only filters when its directory part IS the current directory;
  // otherwise the user is typing a path elsewhere (navigation, not a filter).
  const dirText = trimmed.slice(0, slash) || "/";
  return normalizeTypedPath(dirText, home) === currentAbsolute ? partial : null;
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
        <TooltipTrigger asChild>
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

interface WorkspacePickerProps {
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
   * ``"/Users/corey/projects"``. ``undefined`` starts at the host's
   * home directory. Read only at mount time; later changes are
   * ignored (navigate via the picker UI instead).
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
}

/**
 * Flat-list directory picker for choosing a workspace.
 *
 * A header (up / home / editable path / show-hidden / select / close)
 * sits above the current directory's entries; clicking a folder
 * navigates into it. The header "Select" button picks the directory
 * currently shown — kept in the always-visible header so it doesn't
 * fall below the fold on short screens. Files are grayed out —
 * workspaces must be directories.
 *
 * @param hostId Host whose filesystem to browse.
 * @param onSelect Fired with the current directory on "Select
 *   current". Omit to hide that button.
 * @param onClose Fired when the ✕ button is clicked.
 * @param onNavigate Fired with the current directory on every navigation,
 *   for a live-updating picker with no "Select" button.
 * @param initialPath Absolute path to open at on mount; defaults to
 *   the host's home directory.
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
}: WorkspacePickerProps) {
  // "" means home — the server forwards ~ to list_dir. initialPath
  // seeds the start dir (read once at mount).
  const [path, setPath] = useState<string>(initialPath ?? "");
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
  const createDir = useCreateHostDirectory();

  // Reset to home when the host *changes* — a path from the old host
  // is meaningless on the new one. Compare the previous hostId rather
  // than a "first run" flag: the latter resets on mount under
  // StrictMode's double-invoke and clobbers the ``initialPath`` seed.
  const prevHostId = useRef(hostId);
  useEffect(() => {
    if (prevHostId.current === hostId) return;
    prevHostId.current = hostId;
    setPath("");
    setPathInput("");
    setResolvedHome(null);
    userEditedRef.current = false;
    setNewFolderName(null);
    setCreateError(null);
  }, [hostId]);

  const { data, isLoading, error, isPlaceholderData } = useHostFilesystem(hostId, path);

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
    const idx = first.path.lastIndexOf("/");
    if (idx > 0) {
      setResolvedHome(first.path.slice(0, idx));
    } else if (idx === 0) {
      setResolvedHome("/");
    }
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
  const currentAbsolute = path.startsWith("/") ? path : (listedAbsolute ?? path);

  // Other live agents working in the directory currently shown. Only a
  // resolved absolute path can match a stored workspace; the home view ("")
  // and unresolved paths report no conflict.
  const occupiedCount =
    occupancyForPath && currentAbsolute.startsWith("/") ? occupancyForPath(currentAbsolute) : 0;

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
    if (currentAbsolute.startsWith("/")) {
      onNavigateRef.current?.(currentAbsolute);
    }
  }, [currentAbsolute]);

  const parent = parentOf(currentAbsolute);

  // Live filter from the path-bar text (shell-style: type a fragment to
  // narrow the current directory). Null when not filtering.
  const activeFilter = listingFilter(pathInput, currentAbsolute, resolvedHome);
  // Typing a dot-prefixed fragment reveals hidden entries even with the
  // toggle off, so ".env" can be found without flipping "Show hidden".
  const includeHidden = showHidden || (activeFilter?.startsWith(".") ?? false);

  // Directories first, then files, alphabetical. Dot-prefixed entries
  // are hidden unless "Show hidden" is on; the active filter narrows by a
  // case-insensitive name prefix.
  const entries = (data?.entries ?? [])
    .filter((e) => includeHidden || !e.name.startsWith("."))
    .filter(
      (e) => activeFilter === null || e.name.toLowerCase().startsWith(activeFilter.toLowerCase()),
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
    setPath(next);
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
  const createBaseDir = currentAbsolute.startsWith("/")
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

  return (
    <div
      className="flex max-h-80 min-h-0 flex-col rounded-md border"
      data-testid="workspace-picker"
    >
      <div className="flex shrink-0 items-center gap-1.5 border-b px-2 py-1.5">
        <PickerIconButton
          label="Up one level"
          icon={<ArrowUpIcon className="size-4" />}
          onClick={() => parent !== null && navigateTo(parent)}
          disabled={parent === null}
          testId="workspace-picker-up"
        />
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
          placeholder="~"
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          className="min-w-0 flex-1 bg-transparent text-sm text-muted-foreground focus:outline-none"
          data-testid="workspace-picker-path-input"
        />
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
            disabled={currentAbsolute === "" || currentAbsolute === null}
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
              <CheckIcon className="size-4" />
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
            {activeFilter !== null ? "No matching entries" : "(empty directory)"}
          </div>
        )}
        {entries.map((entry) => {
          const isDir = entry.type === "directory";
          return (
            <button
              key={entry.path}
              type="button"
              disabled={!isDir}
              // preventDefault keeps focus on the path input so a click while
              // a filter is typed doesn't blur → commit → re-sort the list out
              // from under the click. onClick still does the navigation (and
              // fires for keyboard activation, where mousedown doesn't).
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
