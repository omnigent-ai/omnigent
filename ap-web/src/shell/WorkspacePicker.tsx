import {
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
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
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
}: WorkspacePickerProps) {
  const { t } = useTranslation("nav");
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

  // Once the home listing comes back, derive the home dir's
  // absolute path from the first entry's parent. Only first
  // entry — they all share the same parent. Skip placeholder data
  // (the prior directory kept on screen during a load) or we'd
  // derive home from the wrong directory's entries.
  useEffect(() => {
    if (
      path === "" &&
      resolvedHome === null &&
      !isPlaceholderData &&
      data &&
      data.entries.length > 0
    ) {
      const first = data.entries[0];
      // first.path is "/Users/corey/x" → parent is "/Users/corey".
      const idx = first.path.lastIndexOf("/");
      if (idx > 0) {
        setResolvedHome(first.path.slice(0, idx));
      } else if (idx === 0) {
        setResolvedHome("/");
      }
    }
  }, [path, resolvedHome, data, isPlaceholderData]);

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
      setCreateError(err instanceof Error ? err.message : t("workspacePickerCreateFolderFailed"));
    }
  }

  return (
    <div
      className="flex max-h-80 min-h-0 flex-col rounded-md border"
      data-testid="workspace-picker"
    >
      <div className="flex shrink-0 items-center gap-1.5 border-b px-2 py-1.5">
        <button
          type="button"
          onClick={() => parent !== null && navigateTo(parent)}
          disabled={parent === null}
          aria-label={t("workspacePickerUpOneLevel")}
          title={t("workspacePickerUpOneLevel")}
          className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground disabled:opacity-30"
          data-testid="workspace-picker-up"
        >
          <ArrowUpIcon className="size-4" />
        </button>
        <button
          type="button"
          onClick={() => navigateTo("")}
          aria-label={t("workspacePickerHome")}
          title={t("workspacePickerHome")}
          className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground"
          data-testid="workspace-picker-home"
        >
          <HomeIcon className="size-4" />
        </button>
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
          className="min-w-0 flex-1 bg-transparent text-xs text-muted-foreground focus:outline-none"
          data-testid="workspace-picker-path-input"
        />
        <button
          type="button"
          onClick={() => setShowHidden((v) => !v)}
          aria-label={showHidden ? t("workspacePickerHideHidden") : t("workspacePickerShowHidden")}
          aria-pressed={showHidden}
          title={showHidden ? t("workspacePickerHideHidden") : t("workspacePickerShowHidden")}
          className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground"
          data-testid="workspace-picker-show-hidden"
        >
          {showHidden ? <EyeIcon className="size-4" /> : <EyeOffIcon className="size-4" />}
        </button>
        <button
          type="button"
          onClick={openNewFolder}
          disabled={!canCreateFolder}
          aria-label={t("workspacePickerNewFolder")}
          title={t("workspacePickerNewFolder")}
          className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground disabled:opacity-30"
          data-testid="workspace-picker-new-folder"
        >
          <FolderPlusIcon className="size-4" />
        </button>
        {onSelect && (
          <Button
            type="button"
            size="sm"
            disabled={currentAbsolute === "" || currentAbsolute === null}
            onClick={handleSelect}
            title={t("workspacePickerSelectFolderTitle", { name: basename(currentAbsolute) })}
            className="shrink-0"
            data-testid="workspace-picker-select"
          >
            <CheckIcon className="size-3.5" />
            {t("workspacePickerSelect")}
          </Button>
        )}
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            aria-label={t("workspacePickerClose")}
            title={t("workspacePickerClose")}
            className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground"
            data-testid="workspace-picker-close"
          >
            <XIcon className="size-4" />
          </button>
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
              // eslint-disable-next-line jsx-a11y/no-autofocus -- focus belongs on
              // the field the user just opened; the picker is already a focus trap.
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
              placeholder={t("workspacePickerNewFolderNamePlaceholder")}
              spellCheck={false}
              autoCapitalize="off"
              autoCorrect="off"
              className="min-w-0 flex-1 bg-transparent text-xs text-foreground focus:outline-none"
              data-testid="workspace-picker-new-folder-input"
            />
            <button
              type="button"
              disabled={newFolderName.trim() === "" || createDir.isPending}
              onClick={() => void commitNewFolder()}
              aria-label={t("workspacePickerCreateFolder")}
              title={t("workspacePickerCreateFolder")}
              className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground disabled:opacity-30"
              data-testid="workspace-picker-new-folder-create"
            >
              <CheckIcon className="size-4" />
            </button>
            <button
              type="button"
              onClick={cancelNewFolder}
              aria-label={t("workspacePickerCancelNewFolder")}
              title={t("cancel")}
              className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground"
              data-testid="workspace-picker-new-folder-cancel"
            >
              <XIcon className="size-4" />
            </button>
          </div>
          {createError !== null && (
            <span
              className="text-xs text-destructive"
              data-testid="workspace-picker-new-folder-error"
            >
              {createError}
            </span>
          )}
        </div>
      )}
      {occupiedCount > 0 && (
        <div
          className="flex shrink-0 items-start gap-1.5 border-b bg-warning/10 px-3 py-2 text-xs text-warning"
          data-testid="workspace-picker-conflict"
        >
          <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
          <span>{t("workspacePickerConflict", { count: occupiedCount })}</span>
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-y-auto">
        {isLoading && (
          <div className="px-3 py-3 text-xs text-muted-foreground">{t("loading")}</div>
        )}
        {error !== null && error !== undefined && !isLoading && (
          <div className="px-3 py-3 text-xs text-destructive" data-testid="workspace-picker-error">
            {error instanceof Error ? error.message : t("workspacePickerLoadDirectoryFailed")}
          </div>
        )}
        {!isLoading && error === null && entries.length === 0 && (
          <div className="px-3 py-3 text-xs text-muted-foreground">
            {activeFilter !== null
              ? t("workspacePickerNoMatchingEntries")
              : t("workspacePickerEmptyDirectory")}
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
                "flex w-full items-center gap-2 border-b px-3 py-2 text-left text-xs last:border-b-0 " +
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
            className="px-3 py-2 text-xs text-muted-foreground"
            data-testid="workspace-picker-truncated"
          >
            {t("workspacePickerTruncated")}
          </div>
        )}
      </div>
    </div>
  );
}
