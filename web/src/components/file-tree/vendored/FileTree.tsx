import {
  type CSSProperties,
  Fragment,
  type KeyboardEvent,
  type MouseEvent,
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import "./file-tree.css";
import {
  effectiveGitStatus,
  GIT_STATUS_DESCENDANT_TITLE,
  GIT_STATUS_LABEL,
  GIT_STATUS_TITLE,
} from "./gitStatus";
import { Icon } from "./Icon";
import { MiddleTruncate } from "./MiddleTruncate";
import { FILE_TREE_SPRITE_SHEET, resolveFileIcon } from "./icons";
import { FileTreeModel } from "./model";
import { computeFileTreeRowClickPlan } from "./rowClickPlan";
import { computeScrollTarget, computeWindow } from "./scroll";
import type { FileTreeOptions, FileTreeRowDecoration, FileTreeVisibleRow } from "./types";

export const FILE_TREE_ITEM_HEIGHT = 30;
/** Matches a rendered tree's root element, which holds its rows. */
export const FILE_TREE_ROOT_SELECTOR = "[data-file-tree]";
const OVERSCAN = 10;
const FALLBACK_VIEWPORT_HEIGHT = 420;

type ScrollOffset = "top" | "center" | "nearest";
/** A reveal to replay once the tree has a size, by path since rows can move meanwhile. */
interface PendingReveal {
  path: string;
  offset: ScrollOffset;
}

/** The element's laid-out height; zero while it's hidden or not laid out yet. */
function measuredHeight(scrollElement: HTMLElement): number {
  return scrollElement.getBoundingClientRect().height || scrollElement.clientHeight;
}

/**
 * Scroll `scrollElement` to show row `index`. True only once a measured viewport shows it: a
 * hidden element ignores the write, and a guessed height can't say the row is in view.
 */
function scrollRowIntoView(
  scrollElement: HTMLElement,
  request: { index: number; offset: ScrollOffset; totalRows: number; fallbackHeight: number },
): boolean {
  const height = measuredHeight(scrollElement);
  const target = computeScrollTarget({
    currentScrollTop: scrollElement.scrollTop,
    index: request.index,
    itemHeight: FILE_TREE_ITEM_HEIGHT,
    offset: request.offset,
    totalHeight: request.totalRows * FILE_TREE_ITEM_HEIGHT,
    viewportHeight: height || request.fallbackHeight,
  });
  if (target !== null) scrollElement.scrollTop = target;
  // Browsers snap scrollTop to device pixels, so a fractional target lands within a pixel.
  return height > 0 && (target === null || Math.abs(scrollElement.scrollTop - target) < 1);
}

/**
 * Creates the tree model once for the component's lifetime. Later changes to
 * `options` are ignored; drive the model through its methods instead.
 */
export function useFileTree(options: FileTreeOptions): { model: FileTreeModel } {
  // The model only subscribes to its own path store, so it needs no teardown;
  // destroying it on unmount would break StrictMode's remount check.
  const [model] = useState(() => new FileTreeModel(options));
  return { model };
}

export interface FileTreeProps {
  model: FileTreeModel;
  className?: string;
  style?: CSSProperties;
  "aria-label"?: string;
  /** Text after a row's name, such as line counts; rows repaint with the model. */
  renderRowDecoration?: (row: FileTreeVisibleRow) => FileTreeRowDecoration | null;
  /**
   * Opens the host's menu for a row: from a right-click (anchored at the
   * pointer), Shift+F10 or the context menu key, or the actions button that
   * shows on the hovered or focused row.
   */
  onRowMenu?: (row: FileTreeVisibleRow, anchor: DOMRect) => void;
}

/**
 * A virtualized, read-only file tree. Rows are buttons in a roving-tabindex
 * `tree`, so one Tab stop enters the tree and arrow keys move within it.
 */
export function FileTree({
  model,
  className,
  style,
  "aria-label": ariaLabel,
  renderRowDecoration,
  onRowMenu,
}: FileTreeProps) {
  const subscribe = useCallback((listener: () => void) => model.subscribe(listener), [model]);
  useSyncExternalStore(subscribe, () => model.getVersion());
  useFileTreeSpriteSheet();

  const scrollRef = useRef<HTMLDivElement>(null);
  const rowRefs = useRef(new Map<string, HTMLButtonElement>());
  const ownsFocus = useRef(false);
  const lastFocusedPath = useRef<string | null | undefined>(undefined);
  const [scrollTop, setScrollTop] = useState(0);
  // A reveal the browser couldn't apply, as while the tree is hidden; retried once it has a size.
  const pendingScroll = useRef<PendingReveal | null>(null);
  const [viewportHeight, setViewportHeight] = useState(FALLBACK_VIEWPORT_HEIGHT);
  const [hoveredPath, setHoveredPath] = useState<string | null>(null);
  const [focusWithin, setFocusWithin] = useState(false);

  const itemHeight = FILE_TREE_ITEM_HEIGHT;
  const visibleCount = model.getVisibleCount();
  const totalHeight = visibleCount * itemHeight;
  const focusedIndex = model.getFocusedIndex();
  const focusedPath = model.getFocusedPath();
  const rowWindow = computeWindow({
    itemCount: visibleCount,
    itemHeight,
    overscan: OVERSCAN,
    scrollTop,
    viewportHeight,
  });
  const rows = model.getVisibleRows(rowWindow.start, rowWindow.end);
  // Keep the focused row mounted while it is scrolled out of the window so a
  // wheel scroll does not drop keyboard focus to the page. It shares the rows'
  // keyed list, so React moves the focused button instead of remounting it.
  const parkedRow =
    focusedIndex >= 0 && (focusedIndex < rowWindow.start || focusedIndex > rowWindow.end)
      ? model.getVisibleRows(focusedIndex, focusedIndex)[0]
      : undefined;
  const renderedRows = !parkedRow
    ? rows
    : parkedRow.index < rowWindow.start
      ? [parkedRow, ...rows]
      : [...rows, parkedRow];
  // The indent guide of the focused row's parent stays visible.
  const focusedRow =
    parkedRow ??
    rows.find((row) => row.isFocused) ??
    (focusedIndex >= 0 ? model.getVisibleRows(focusedIndex, focusedIndex)[0] : undefined);
  const activeGuidePath = focusedRow?.ancestorPaths.at(-1);

  useLayoutEffect(() => {
    const scrollElement = scrollRef.current;
    if (!scrollElement) return;
    const measure = () => {
      const height = measuredHeight(scrollElement);
      if (height > 0) setViewportHeight(height);
      const pending = pendingScroll.current;
      if (height > 0 && pending) {
        // The tree may have changed while hidden; a removed row has nothing to reveal.
        const index = model.getVisibleIndexOfPath(pending.path);
        const revealed =
          index < 0 ||
          scrollRowIntoView(scrollElement, {
            index,
            offset: pending.offset,
            totalRows: model.getVisibleCount(),
            fallbackHeight: height,
          });
        if (revealed) pendingScroll.current = null;
      }
      // Showing a hidden tree can move its scroll offset without a scroll event.
      setScrollTop(scrollElement.scrollTop);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(scrollElement);
    return () => observer.disconnect();
  }, [model]);

  // Applies scroll requests from `scrollToPath`, and keeps the focused row in
  // view when keyboard focus moves while the tree owns DOM focus.
  useLayoutEffect(() => {
    const scrollElement = scrollRef.current;
    if (!scrollElement) return;
    const scrollTo = (index: number, path: string | null, offset: ScrollOffset) => {
      const revealed = scrollRowIntoView(scrollElement, {
        index,
        offset,
        totalRows: visibleCount,
        fallbackHeight: viewportHeight,
      });
      // Draw at the offset the browser applied: a hidden tree ignores the write.
      setScrollTop(scrollElement.scrollTop);
      pendingScroll.current = revealed || path === null ? null : { path, offset };
    };

    const request = model.getScrollRequest();
    if (request) {
      model.clearScrollRequest(request.id);
      scrollTo(request.visibleIndex, request.path, request.offset);
      lastFocusedPath.current = focusedPath;
      return;
    }

    const isFirstRun = lastFocusedPath.current === undefined;
    const focusChanged = lastFocusedPath.current !== focusedPath;
    lastFocusedPath.current = focusedPath;
    if (isFirstRun) {
      // Reveal an initially selected row without taking focus.
      scrollTo(focusedIndex, focusedPath, "nearest");
      return;
    }
    if (!ownsFocus.current || !focusChanged || focusedPath === null) return;
    scrollTo(focusedIndex, focusedPath, "nearest");
    rowRefs.current.get(focusedPath)?.focus({ preventScroll: true });
  });

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const opensMenu = event.key === "ContextMenu" || (event.shiftKey && event.key === "F10");
    if (opensMenu && onRowMenu) {
      // Read focus as it is now: the key may arrive before a re-render.
      const index = model.getFocusedIndex();
      const path = model.getFocusedPath();
      const row = index < 0 ? undefined : model.getVisibleRows(index, index)[0];
      const element = path === null ? undefined : rowRefs.current.get(path);
      if (row && element) {
        event.preventDefault();
        event.stopPropagation();
        const box = element.getBoundingClientRect();
        onRowMenu(row, new DOMRect(box.left, box.top, box.width, box.height));
      }
      return;
    }
    if (handleTreeKey(model, event)) {
      event.preventDefault();
      event.stopPropagation();
    }
  };

  const registerRow = (path: string, element: HTMLButtonElement | null) => {
    if (element) rowRefs.current.set(path, element);
    else rowRefs.current.delete(path);
  };

  // One actions button follows the hovered row, or the focused one while the tree has focus.
  const menuRow = !onRowMenu
    ? undefined
    : (renderedRows.find((row) => targetPath(row) === hoveredPath) ??
      (focusWithin ? focusedRow : undefined));
  const rowProps = { model, itemHeight, registerRow, renderRowDecoration, onRowMenu };
  return (
    <div
      data-file-tree=""
      data-file-tree-colored-icons={model.coloredIcons ? "" : undefined}
      className={className}
      style={style}
      onFocus={() => {
        ownsFocus.current = true;
        setFocusWithin(true);
      }}
      onBlur={(event) => {
        if (event.currentTarget.contains(event.relatedTarget)) return;
        ownsFocus.current = false;
        setFocusWithin(false);
      }}
      onPointerMove={(event) => {
        if (!onRowMenu) return;
        const target = event.target instanceof Element ? event.target : null;
        if (target?.closest('[data-type="context-menu-trigger"]')) return;
        const row = target?.closest<HTMLElement>('[role="treeitem"][data-item-path]');
        setHoveredPath(row?.getAttribute("data-item-path") ?? null);
      }}
      onPointerLeave={() => setHoveredPath(null)}
    >
      <div
        ref={scrollRef}
        data-file-tree-scroll=""
        onScroll={(event) => {
          // Once the tree has a size, a scroll by the reader replaces any reveal still waiting.
          if (measuredHeight(event.currentTarget) > 0) pendingScroll.current = null;
          setScrollTop(event.currentTarget.scrollTop);
        }}
      >
        <div data-file-tree-list="" style={{ height: totalHeight }}>
          <div
            role="tree"
            aria-label={ariaLabel}
            aria-multiselectable
            data-file-tree-window=""
            style={{ transform: `translateY(${rowWindow.start * itemHeight}px)` }}
            onKeyDown={handleKeyDown}
          >
            {renderedRows.map((row) => (
              <FileTreeRow
                key={row.path}
                row={row}
                parkedTop={
                  row === parkedRow ? (row.index - rowWindow.start) * itemHeight : undefined
                }
                activeGuidePath={activeGuidePath}
                hasMenuButton={menuRow?.path === row.path}
                {...rowProps}
              />
            ))}
          </div>
          {menuRow && onRowMenu && (
            <button
              type="button"
              data-type="context-menu-trigger"
              aria-label={`Actions for ${rowLabel(menuRow)}`}
              aria-haspopup="menu"
              tabIndex={-1}
              style={{ top: menuRow.index * itemHeight, height: itemHeight }}
              // Keeps focus on the row, so the menu returns it there.
              onMouseDown={(event) => event.preventDefault()}
              onClick={(event) => {
                event.preventDefault();
                event.stopPropagation();
                onRowMenu(menuRow, event.currentTarget.getBoundingClientRect());
              }}
            >
              <Icon name="file-tree-icon-ellipsis" />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// Returns whether the key was handled. Enter and Space reach rows as native
// button clicks instead.
function handleTreeKey(model: FileTreeModel, event: KeyboardEvent): boolean {
  const command = event.metaKey || event.ctrlKey;
  if (event.shiftKey && event.key === "ArrowDown") model.extendSelectionFromFocused(1);
  else if (event.shiftKey && event.key === "ArrowUp") model.extendSelectionFromFocused(-1);
  else if (command && event.key === " ") model.toggleFocusedSelection();
  else if (command && event.key.toLowerCase() === "a") model.selectAllVisiblePaths();
  else if (command || event.altKey) return false;
  else {
    const focused = model.getFocusedItem();
    const directory = focused && "expand" in focused ? focused : null;
    switch (event.key) {
      case "ArrowDown":
        model.focusNextItem();
        break;
      case "ArrowUp":
        model.focusPreviousItem();
        break;
      case "ArrowRight":
        if (directory && !directory.isExpanded()) directory.expand();
        else model.focusNextItem();
        break;
      case "ArrowLeft":
        if (directory?.isExpanded()) directory.collapse();
        else model.focusParentItem();
        break;
      case "Home":
        model.focusFirstItem();
        break;
      case "End":
        model.focusLastItem();
        break;
      default:
        return false;
    }
  }
  return true;
}

function FileTreeRow({
  model,
  row,
  itemHeight,
  parkedTop,
  activeGuidePath,
  hasMenuButton = false,
  registerRow,
  renderRowDecoration,
  onRowMenu,
}: {
  model: FileTreeModel;
  row: FileTreeVisibleRow;
  itemHeight: number;
  parkedTop?: number;
  activeGuidePath?: string;
  /** The actions button sits on this row, so the row keeps room for it. */
  hasMenuButton?: boolean;
  registerRow: (path: string, element: HTMLButtonElement | null) => void;
  renderRowDecoration?: FileTreeProps["renderRowDecoration"];
  onRowMenu?: FileTreeProps["onRowMenu"];
}) {
  const path = targetPath(row);
  const gitStatus = model.gitStatus;
  const status = gitStatus ? effectiveGitStatus(gitStatus, path, row.ancestorPaths) : null;
  const containsChange =
    row.kind === "directory" &&
    status === null &&
    (gitStatus?.directoriesWithChanges.has(path) ?? false);
  const label = rowLabel(row);
  const decoration = renderRowDecoration?.(row) ?? null;

  const handleClick = (event: MouseEvent<HTMLButtonElement>) => {
    const plan = computeFileTreeRowClickPlan(event, row.kind === "directory");
    if (plan.toggleDirectory && model.resolveMountedDirectoryPathFromInput(path) === null) return;
    if (plan.selection.kind === "range") model.selectPathRange(path, plan.selection.additive);
    else if (plan.selection.kind === "toggle") model.togglePathSelectionFromInput(path);
    else model.selectOnlyMountedPathFromInput(path);
    model.focusMountedPathFromInput(path);
    if (plan.toggleDirectory) model.toggleMountedDirectoryFromInput(path);
    // WebKit does not focus buttons on click; focus the row so arrow keys work
    // next. While searching, the host's search field keeps focus instead.
    if (!model.isSearchOpen()) event.currentTarget.focus({ preventScroll: true });
  };

  return (
    <button
      ref={(element) => registerRow(path, element)}
      type="button"
      role="treeitem"
      tabIndex={row.isFocused ? 0 : -1}
      aria-label={label}
      aria-level={row.depth + 1}
      aria-posinset={row.posInSet + 1}
      aria-setsize={row.setSize}
      aria-selected={row.isSelected}
      aria-expanded={row.kind === "directory" ? row.isExpanded : undefined}
      data-type="item"
      data-item-path={path}
      data-item-type={row.kind === "directory" ? "folder" : "file"}
      data-item-git-status={status ?? undefined}
      data-item-contains-git-change={containsChange ? "" : undefined}
      data-item-has-menu-button={hasMenuButton ? "" : undefined}
      style={
        parkedTop === undefined
          ? undefined
          : {
              position: "absolute",
              top: parkedTop,
              left: 0,
              right: 0,
              height: itemHeight,
            }
      }
      // While searching, keep DOM focus in the host's search field.
      onMouseDown={(event) => {
        if (model.isSearchOpen()) event.preventDefault();
      }}
      onFocus={() => model.focusMountedPathFromInput(path)}
      onClick={handleClick}
      onContextMenu={
        onRowMenu &&
        ((event) => {
          event.preventDefault();
          model.focusMountedPathFromInput(path);
          if (!model.isSearchOpen()) event.currentTarget.focus({ preventScroll: true });
          onRowMenu(row, new DOMRect(event.clientX, event.clientY, 0, 0));
        })
      }
    >
      {row.depth > 0 && (
        <span data-item-section="spacing">
          {row.ancestorPaths.map((ancestor) => (
            <span
              key={ancestor}
              data-item-section="spacing-item"
              data-active-guide={ancestor === activeGuidePath ? "" : undefined}
            />
          ))}
        </span>
      )}
      <span data-item-section="icon">
        {row.kind === "directory" ? (
          <Icon name="file-tree-icon-chevron" />
        ) : (
          <Icon {...resolveFileIcon(path)} />
        )}
      </span>
      <span data-item-section="content">
        {row.flattenedSegments && row.flattenedSegments.length > 0 ? (
          <span data-item-flattened-subitems="">
            {row.flattenedSegments.map((segment, index) => (
              <Fragment key={segment.path}>
                {index > 0 && " / "}
                <span>{segment.name}</span>
              </Fragment>
            ))}
          </span>
        ) : (
          <MiddleTruncate name={row.name} />
        )}
      </span>
      {decoration && (
        <span data-item-section="decoration">
          <span title={decoration.title}>
            {decoration.parts
              ? decoration.parts.map((part) => (
                  <span key={part.text} style={{ color: part.color }}>
                    {part.text}
                  </span>
                ))
              : decoration.text}
          </span>
        </span>
      )}
      {gitStatus && <GitLane status={status} containsChange={containsChange} />}
    </button>
  );
}

function GitLane({
  status,
  containsChange,
}: {
  status: ReturnType<typeof effectiveGitStatus>;
  containsChange: boolean;
}) {
  const label = status === null ? null : GIT_STATUS_LABEL[status];
  return (
    <span data-item-section="git">
      {label !== null ? (
        <span title={GIT_STATUS_TITLE[status!]}>{label}</span>
      ) : (
        containsChange && (
          <span title={GIT_STATUS_DESCENDANT_TITLE}>
            <Icon name="file-tree-icon-dot" size={6} />
          </span>
        )
      )}
    </span>
  );
}

// How a row reads aloud: its name, or a merged chain as "src / utils".
function rowLabel(row: FileTreeVisibleRow): string {
  return row.flattenedSegments?.map((segment) => segment.name).join(" / ") ?? row.name;
}

// A flattened row ("src / utils / deep") acts on its deepest directory.
function targetPath(row: FileTreeVisibleRow): string {
  return row.isFlattened
    ? (row.flattenedSegments?.filter((segment) => segment.isTerminal).at(-1)?.path ?? row.path)
    : row.path;
}

let spriteSheetUsers = 0;
let spriteSheetElement: Element | null = null;

// Icons reference `<symbol>`s by id, so the sprite sheet is mounted once per
// document and shared by every tree and every icon drawn outside one.
export function useFileTreeSpriteSheet() {
  useLayoutEffect(() => {
    spriteSheetUsers += 1;
    if (!spriteSheetElement) {
      const template = document.createElement("template");
      template.innerHTML = FILE_TREE_SPRITE_SHEET;
      spriteSheetElement = template.content.firstElementChild;
      spriteSheetElement?.setAttribute(
        "style",
        "position: absolute; width: 0; height: 0; overflow: hidden",
      );
      if (spriteSheetElement) document.body.append(spriteSheetElement);
    }
    return () => {
      spriteSheetUsers -= 1;
      if (spriteSheetUsers > 0) return;
      spriteSheetElement?.remove();
      spriteSheetElement = null;
    };
  }, []);
}
