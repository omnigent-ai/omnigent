import { useEffect, useRef, useState } from "react";
import { FolderIcon, FolderOpenIcon } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useHostFilesystem } from "@/hooks/useHostFilesystem";
import { isNavigablePath } from "./WorkspacePicker";

// DOM-safety bound only; the dropdown scrolls and overflow is
// surfaced ("+N more") rather than silently dropped.
const MATCH_DISPLAY_LIMIT = 100;

/**
 * Split a typed path into the directory to list and the partial
 * basename to filter it by — drives the "Matches" autocomplete.
 *
 * Home is ``""`` (the endpoint's "list home" form); a bare ``"~"``,
 * the empty input, and a leading ``"~/"`` all map there.
 *
 * @param input Whatever the user has typed, e.g.
 *   ``"/home/serena.ruan/agent"`` or ``"~/proj"`` or ``""``.
 * @returns ``{ dir, partial }`` — ``dir`` is the directory to list
 *   (``""`` for home), ``partial`` the basename prefix to match.
 */
export function splitTypedPath(input: string): { dir: string; partial: string } {
  const trimmed = input.trim();
  // Home shortcuts — nothing to filter yet.
  if (trimmed === "" || trimmed === "~") {
    return { dir: "", partial: "" };
  }
  const slash = trimmed.lastIndexOf("/");
  if (slash === -1) {
    // Bare fragment with no directory part (e.g. "proj"); filter
    // home by it. Selection still requires an absolute path.
    return { dir: "", partial: trimmed };
  }
  const partial = trimmed.slice(slash + 1);
  let dir = trimmed.slice(0, slash);
  if (dir === "") {
    // "/foo" → the parent is the filesystem root.
    dir = "/";
  } else if (dir === "~") {
    // "~/foo" → the parent is home.
    dir = "";
  }
  return { dir, partial };
}

interface RowProps {
  path: string;
  active: boolean;
  onSelect: () => void;
  /** Index for stable keyboard-highlight wiring. */
  testId: string;
}

function PathRow({ path, active, onSelect, testId }: RowProps) {
  return (
    <button
      type="button"
      // id doubles as the aria-activedescendant target on the input.
      id={testId}
      role="option"
      aria-selected={active}
      data-active={active}
      // mousedown (not click) so selection fires before the input's
      // blur closes the dropdown.
      onMouseDown={(e) => {
        e.preventDefault();
        onSelect();
      }}
      className={`flex w-full items-center gap-2 px-3 py-2 text-left text-xs transition ${
        active ? "bg-accent text-accent-foreground" : "hover:bg-accent hover:text-accent-foreground"
      }`}
      data-testid={testId}
    >
      <FolderOpenIcon className="size-4 shrink-0 text-muted-foreground" />
      <span className="flex-1 truncate">{path}</span>
    </button>
  );
}

interface WorkspacePathFieldProps {
  /** Host whose filesystem backs the "Matches" group. */
  hostId: string | null;
  /** Current absolute-path value (the workspace). */
  value: string;
  /** Called with the new value on type or selection. */
  onChange: (value: string) => void;
  /** Open the full tree browser (the folder-icon button). */
  onBrowse: () => void;
  /**
   * Commit an absolute path the user typed and pressed Enter on, e.g.
   * ``"/tmp"``. The dialog opens the tree browser at this path so the
   * user sees its contents. Fires only for absolute paths; omit to
   * disable Enter-to-commit.
   */
  onCommit?: (path: string) => void;
  /** Most-recent-first paths for the "Recent" group. */
  recent: string[];
  /** Suppress the dropdown (e.g. while the tree browser is open). */
  dropdownDisabled?: boolean;
}

/**
 * Working-directory combobox: a path input with a dropdown that
 * surfaces recently-used directories and live filesystem matches,
 * plus a folder button that opens the full tree browser.
 *
 * "Recent" comes from per-host localStorage; "Matches" lists the
 * parent of the typed path and filters its sub-directories by the
 * trailing partial (see :func:`splitTypedPath`). Both reuse the
 * existing host filesystem endpoint — no server-side support.
 *
 * @param hostId Host to browse.
 * @param value Current path value.
 * @param onChange Value setter.
 * @param onBrowse Opens the tree browser.
 * @param onCommit Opens the tree browser at a typed absolute path on Enter.
 * @param recent Recent paths for this host.
 */
export function WorkspacePathField({
  hostId,
  value,
  onChange,
  onBrowse,
  onCommit,
  recent,
  dropdownDisabled = false,
}: WorkspacePathFieldProps) {
  const { t } = useTranslation("nav");
  const [open, setOpen] = useState(false);
  // Index into the combined [recent..., matches...] list, or -1 for
  // "nothing highlighted" (typing in the input).
  const [highlight, setHighlight] = useState(-1);
  const containerRef = useRef<HTMLDivElement>(null);

  // Honor the open state only when the dropdown isn't suppressed.
  const dropdownOpen = open && !dropdownDisabled;
  const trimmed = value.trim();
  const { dir, partial } = splitTypedPath(value);

  // List the parent directory only while the dropdown is open.
  const { data, isLoading } = useHostFilesystem(hostId, dropdownOpen ? dir : null);

  // Recent: unfiltered at the home shortcut, else filtered by the
  // typed text so a concrete path doesn't surface stale history.
  const recentFilter = trimmed === "~" ? "" : trimmed;
  const filteredRecent =
    recentFilter === ""
      ? recent
      : recent.filter((p) => p.toLowerCase().includes(recentFilter.toLowerCase()));
  const recentSet = new Set(filteredRecent);

  // Matches: parent's sub-directories starting with the partial,
  // minus those already under Recent. Dot-dirs are hidden until the
  // partial starts with "." (shell tab-completion convention).
  const lowerPartial = partial.toLowerCase();
  const showHidden = partial.startsWith(".");
  const allMatches = (data?.entries ?? [])
    .filter(
      (e) =>
        e.type === "directory" &&
        e.name.toLowerCase().startsWith(lowerPartial) &&
        (showHidden || !e.name.startsWith(".")) &&
        !recentSet.has(e.path),
    )
    .map((e) => e.path);
  const matches = allMatches.slice(0, MATCH_DISPLAY_LIMIT);
  const hiddenMatchCount = allMatches.length - matches.length;

  // Flat list for keyboard navigation across both groups.
  const items = [...filteredRecent, ...matches];
  const showLoading = dropdownOpen && isLoading && matches.length === 0;
  const hasContent = filteredRecent.length > 0 || matches.length > 0 || showLoading;

  // DOM id of the highlighted row, for aria-activedescendant. The
  // first filteredRecent.length items are "recent", the rest "match"
  // — mirror the testId/id scheme the rows render with.
  const activeDescendantId =
    highlight < 0
      ? undefined
      : highlight < filteredRecent.length
        ? `workspace-recent-${highlight}`
        : `workspace-match-${highlight - filteredRecent.length}`;

  // Close on click outside while open.
  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
        setHighlight(-1);
      }
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  // Keep the keyboard-highlighted row visible when it scrolls past
  // the dropdown's fold.
  useEffect(() => {
    if (highlight < 0 || !containerRef.current) return;
    const el = containerRef.current.querySelector('[data-active="true"]');
    el?.scrollIntoView({ block: "nearest" });
  }, [highlight]);

  function select(path: string) {
    onChange(path);
    setOpen(false);
    setHighlight(-1);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!open) {
        setOpen(true);
        return;
      }
      if (items.length > 0) {
        setHighlight((h) => Math.min(h + 1, items.length - 1));
      }
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight((h) => Math.max(h - 1, -1));
    } else if (e.key === "Enter") {
      if (open && highlight >= 0 && highlight < items.length) {
        // A dropdown row is highlighted — pick it.
        e.preventDefault();
        select(items[highlight]);
      } else if (trimmed !== "") {
        // No row highlighted: commit a navigable path (absolute or
        // ~-relative, which the host expands). The dialog (re)mounts the
        // tree browser at it, so Enter navigates there whether the browser
        // is open or closed — matching the old picker's navigate-on-Enter.
        e.preventDefault();
        setOpen(false);
        setHighlight(-1);
        if (isNavigablePath(trimmed)) {
          onCommit?.(trimmed);
        }
      }
    } else if (e.key === "Escape") {
      if (open) {
        e.preventDefault();
        setOpen(false);
        setHighlight(-1);
      }
    }
  }

  return (
    <div ref={containerRef} className="relative">
      <div className="flex items-center gap-2">
        <input
          type="text"
          value={value}
          onChange={(e) => {
            onChange(e.target.value);
            setOpen(true);
            setHighlight(-1);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={onKeyDown}
          placeholder={t("workspacePathPlaceholder")}
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          role="combobox"
          aria-label={t("workspacePathLabel")}
          aria-autocomplete="list"
          aria-expanded={dropdownOpen}
          aria-controls="workspace-path-listbox"
          aria-activedescendant={dropdownOpen ? activeDescendantId : undefined}
          className="flex-1 rounded-md border border-input bg-background px-3 py-2 text-xs outline-none transition-colors focus-visible:border-ring"
          data-testid="workspace-path-input"
        />
        <button
          type="button"
          onClick={onBrowse}
          aria-label={t("browseDirectories")}
          className="flex size-9 shrink-0 items-center justify-center rounded-md border border-input bg-background text-muted-foreground transition hover:bg-muted hover:text-foreground"
          data-testid="workspace-browse-toggle"
        >
          <FolderIcon className="size-4" />
        </button>
      </div>

      {dropdownOpen && hasContent && (
        <div
          id="workspace-path-listbox"
          role="listbox"
          // Inline (not absolute/portaled): an absolute dropdown is
          // clipped by the dialog's scrollable body, and a body
          // portal can't be clicked through Radix's modal layer.
          // Inline content just scrolls with the form instead.
          className="mt-1 max-h-72 overflow-y-auto rounded-md border border-border bg-popover shadow-md"
          data-testid="workspace-path-dropdown"
        >
          {filteredRecent.length > 0 && (
            <>
              <div className="px-3 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                {t("recent")}
              </div>
              {filteredRecent.map((path, i) => (
                <PathRow
                  key={`recent-${path}`}
                  path={path}
                  active={highlight === i}
                  onSelect={() => select(path)}
                  testId={`workspace-recent-${i}`}
                />
              ))}
            </>
          )}
          {matches.length > 0 && (
            <>
              <div className="px-3 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                {t("matches")}
              </div>
              {matches.map((path, j) => (
                <PathRow
                  key={`match-${path}`}
                  path={path}
                  active={highlight === filteredRecent.length + j}
                  onSelect={() => select(path)}
                  testId={`workspace-match-${j}`}
                />
              ))}
              {hiddenMatchCount > 0 && (
                <div
                  className="px-3 py-2 text-xs text-muted-foreground"
                  data-testid="workspace-match-overflow"
                >
                  {t("matchOverflow", { count: hiddenMatchCount })}
                </div>
              )}
            </>
          )}
          {showLoading && (
            <div className="px-3 py-2 text-xs text-muted-foreground">{t("loading")}</div>
          )}
        </div>
      )}
    </div>
  );
}
