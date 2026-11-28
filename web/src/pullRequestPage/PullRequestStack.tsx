// A stacked PR's place in its stack on the PR page: the header's stack menu, its Previous and
// Next buttons and depends-on chip, Previous and Next in stack in ⌘K, and the Overview's stack card.

import {
  createContext,
  type MouseEvent,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ArrowDownIcon,
  ArrowUpIcon,
  ArrowUpRightIcon,
  ChevronDownIcon,
  ChevronsDownIcon,
  ChevronsUpIcon,
  CircleDashedIcon,
  CircleHelpIcon,
  ExternalLinkIcon,
  LayersIcon,
  LoaderCircleIcon,
} from "lucide-react";

import { KeyboardShortcutTooltip } from "@/components/KeyboardShortcut";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { StackDepthPill } from "@/components/StackDepthPill";
import { CompactShortcutKeys } from "@/components/ui/kbd";
import { openLink } from "@/lib/openLinkInApp";
import { shortcutKeys } from "@/lib/shortcuts/registry";
import type { StackMemberStatus } from "@/lib/pullRequestStacks/stackMembers";
import type { StackReadAhead } from "@/lib/pullRequestStacks/stackWarmup";
import type { StackNode } from "@/lib/pullRequestStacks/stackModel";
import {
  FOLD_ABOVE_ROWS,
  foldStackRows,
  isStraightStack,
  revealStackRows,
  STACK_FOLDED,
  type StackFoldState,
  stackPositionName,
  stackRowKey,
  type StackTreeRow,
} from "@/lib/pullRequestStacks/stackNavigation";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";
import { pullRequestLifecycleVisual } from "@/shell/PullRequestLifecycleBadge";
import {
  isCommandAvailable,
  type PaletteCommand,
  usePaletteCommands,
} from "@/store/paletteCommands";

import {
  type StackEntryTone,
  type StackEntryView,
  type StackLink,
  stackCoverage,
  stackEntryLink,
  stackEntryView,
  type StackMove,
  stackStepCommand,
} from "./pullRequestStackModel";
import type { PullRequestStackView } from "./usePullRequestStack";
import { STACK_SHORTCUTS, usePullRequestStackKeys } from "./usePullRequestStackKeys";
import { useStackReadAheadIntent } from "./useStackReadAheadIntent";
import "./pullRequestStack.css";

const TONE_CLASS: Record<StackEntryTone, string> = {
  open: "text-status-green",
  draft: "text-muted-foreground",
  merged: "text-status-purple",
  closed: "text-status-red",
  approved: "text-status-green",
  changes: "text-status-red",
  muted: "text-muted-foreground",
};

export const STACK_OUT_OF_DATE =
  "Stack info may be out of date: the description's stack section isn't in the usual format.";

/** For a GitHub stack read only in part. */
export const STACK_PARTIAL = "Only part of this stack could be read here.";
export const SEE_WHOLE_STACK = "See the whole stack on GitHub";
export const STACK_LOADING_REST = "Loading the rest of the stack…";
export const STACK_LOADING = "Loading the stack";

/**
 * Opens a stack entry. One in this repository opens where this PR shows when that view can
 * show it, else on the code host; any other opens in the browser.
 */
function useOpenInStack(stack: PullRequestStackView): (link: StackLink) => void {
  const { selectInPlace } = stack;
  return useCallback(
    ({ url, external }: StackLink) => {
      if (!external && selectInPlace(url)) return;
      openLink(undefined, url);
    },
    [selectInPlace],
  );
}

/** What the page, menu and card show of one entry. */
interface ShownEntry {
  node: StackNode;
  status: StackMemberStatus;
  entry: StackEntryView;
  link: StackLink | null;
}

function shownEntry(
  stack: PullRequestStackView,
  node: StackNode,
  status: StackMemberStatus,
): ShownEntry {
  const self = stack.parse.nodes[stack.parse.selfIndex].ref;
  return { node, status, entry: stackEntryView(node, status, self), link: stackEntryLink(node) };
}

function shownDependency(stack: PullRequestStackView): ShownEntry | null {
  const { dependency } = stack.parse;
  const status = stack.statuses.dependency;
  return dependency?.ref && status ? shownEntry(stack, dependency, status) : null;
}

/** The member a row opens here, whose reads its row can move ahead; null for any other. */
function readAheadUrl(shown: ShownEntry, current: boolean): string | null {
  return !current && shown.link && !shown.link.external ? shown.link.url : null;
}

function EntryIcon({ entry, status }: { entry: StackEntryView; status: StackMemberStatus }) {
  const className = cn("size-3.5 shrink-0", TONE_CLASS[entry.tone]);
  if (entry.state) {
    const { Icon } = pullRequestLifecycleVisual(entry.state.state, entry.state.isDraft);
    return <Icon aria-hidden className={className} />;
  }
  if (status.kind === "loading") {
    return <LoaderCircleIcon aria-hidden className={cn(className, "animate-spin")} />;
  }
  if (status.kind === "unpushed") return <CircleDashedIcon aria-hidden className={className} />;
  if (status.kind === "external") return <ArrowUpRightIcon aria-hidden className={className} />;
  return <CircleHelpIcon aria-hidden className={className} />;
}

/** How a stack is marked wherever it's shown: layers, as the code host's own stack button. */
export function StackIcon({ className }: { className?: string }) {
  return <LayersIcon aria-hidden className={cn("size-3.5 shrink-0", className)} />;
}

/**
 * The tree's lines before an entry: a guide per ancestor level, then its own connector. Past
 * the deepest indent, a row shows its level's number.
 */
function TreeGuides({ row }: { row: StackTreeRow }) {
  if (row.connector === null) return null;
  return (
    <span data-stack-tree="" className="flex shrink-0 font-mono text-foreground-tertiary">
      {row.guides
        .map((carries, level) => ({ carries, level }))
        .map((guide) => (
          <span key={guide.level} aria-hidden className="w-3 text-center">
            {guide.carries ? "│" : ""}
          </span>
        ))}
      <span aria-hidden className="w-3 text-center">
        {row.connector === "tee" ? "├" : "└"}
      </span>
      {row.deepLevel !== null && <StackDepthPill level={row.deepLevel} className="mr-0.5" />}
    </span>
  );
}

/** How this PR's row stands out in the menu and the card, in every palette. */
export const CURRENT_ROW_CLASS = "pr-stack-current-row";

/** Muted text in a row; in this PR's, the row's own color, which keeps its contrast. */
const rowDetailClass = (current: boolean) => (current ? undefined : "text-muted-foreground");

/** An entry's status, or "This PR" with its emphasis, for the current one. */
function EntryStatus({ shown, current }: { shown: ShownEntry; current: boolean }) {
  return (
    <span
      title={current ? "This PR" : shown.entry.status}
      className={cn(
        "shrink-0 text-xs",
        current ? "font-medium" : TONE_CLASS[shown.entry.statusTone],
      )}
    >
      {current ? "This PR" : shown.entry.status}
    </span>
  );
}

/**
 * One entry as the card's three columns: what leads it and its title, its number right-aligned
 * in tabular figures, and its status, so every row's number and status line up.
 */
function EntryColumns({
  shown,
  current = false,
  lead,
}: {
  shown: ShownEntry;
  current?: boolean;
  lead: ReactNode;
}) {
  const { entry, status } = shown;
  return (
    <>
      <span className="flex min-w-0 items-center gap-1.5 overflow-hidden">
        {lead}
        <EntryIcon entry={entry} status={status} />
        <span className={cn("min-w-0 flex-1 truncate", current && "font-semibold")}>
          {entry.title}
        </span>
      </span>
      <span
        title={entry.number ?? undefined}
        className={cn("max-w-full justify-self-end truncate tabular-nums", rowDetailClass(current))}
      >
        {entry.number}
      </span>
      <span className="max-w-full justify-self-start truncate">
        <EntryStatus shown={shown} current={current} />
      </span>
    </>
  );
}

function rowEntries(stack: PullRequestStackView) {
  return stack.rows.map((row) => ({
    row,
    shown: shownEntry(stack, row.node, stack.statuses.nodes[row.index]),
    current: row.index === stack.parse.selfIndex,
  }));
}

/**
 * `Stack · 3 PRs`, and how much of their state was read while some of it wasn't: on its own
 * line in the menu, after a separator in the card.
 */
function StackSummary({
  stack,
  inline = false,
}: {
  stack: PullRequestStackView;
  inline?: boolean;
}) {
  // A stack read in part counts what GitHub says it has, not just the rows shown.
  const pullRequests = stack.partial
    ? stack.partial.size
    : stack.parse.nodes.filter((node) => node.ref).length;
  const coverage = stackCoverage(stack.statuses);
  const count =
    pullRequests === null ? "" : ` · ${pullRequests} ${pullRequests === 1 ? "PR" : "PRs"}`;
  return (
    <>
      {/* In a narrow card the summary gives way, so the card's own controls stay whole. */}
      <span
        title={inline ? `Stack${count} · merges from the top` : undefined}
        className={inline ? "min-w-0 truncate" : undefined}
      >
        Stack
        {count}
        {inline && <span className="@max-[24rem]:hidden"> · merges from the top</span>}
      </span>
      {coverage && (
        // On one line, so the card keeps its height as statuses come in, however narrow.
        <span
          data-stack-coverage=""
          title={inline ? coverage : undefined}
          className={inline ? "min-w-0 truncate" : undefined}
        >
          {inline && <span aria-hidden>· </span>}
          {coverage}
        </span>
      )}
    </>
  );
}

/**
 * Previous and Next in stack while the page is active: one step for each, run by its ⌘K row and
 * by ⇧K / ⇧J. Next among several PRs opens the stack menu to pick from, rather than picking one.
 */
function usePullRequestStackCommands(
  stack: PullRequestStackView,
  active: boolean,
  open: (link: StackLink) => void,
  openMenu: () => void,
) {
  const instance = useId();
  const latest = useRef({ open, openMenu });
  useEffect(() => {
    latest.current = { open, openMenu };
  });
  const previous = stackStepCommand("previous", stack.previous);
  const next = stackStepCommand("next", stack.next);
  const first = stackStepCommand("first", stack.first);
  const last = stackStepCommand("last", stack.last);
  const selfUrl = stack.parse.nodes[stack.parse.selfIndex].ref?.url ?? "";
  const signature = JSON.stringify({ previous, next, first, last });
  const steps = useMemo(
    () => JSON.parse(signature) as Record<StackMove, typeof previous>,
    [signature],
  );
  const step = useCallback(
    (direction: StackMove) => {
      const { availability, target } = steps[direction];
      if (!isCommandAvailable(availability)) return;
      if (target === "choose") latest.current.openMenu();
      else if (target) latest.current.open({ url: target, external: false });
    },
    [steps],
  );
  const moves = useMemo<Record<StackMove, PaletteCommand>>(() => {
    const move = (
      direction: StackMove,
      extra: Pick<PaletteCommand, "icon" | "keywords"> & Partial<PaletteCommand>,
    ): PaletteCommand => ({
      id: `pull-request-stack:${direction}:${selfUrl}`,
      label: steps[direction].label,
      availability: steps[direction].availability,
      movesFocus: true,
      run: () => step(direction),
      ...extra,
    });
    return {
      previous: move("previous", {
        icon: ArrowUpIcon,
        keywords: ["stack", "stacked", "pull request", "parent"],
        shortcut: STACK_SHORTCUTS.previous,
      }),
      next: move("next", {
        icon: ArrowDownIcon,
        keywords: ["stack", "stacked", "pull request", "child"],
        shortcut: STACK_SHORTCUTS.next,
      }),
      first: move("first", {
        icon: ChevronsUpIcon,
        keywords: ["stack", "stacked", "pull request", "base", "bottom"],
      }),
      last: move("last", {
        icon: ChevronsDownIcon,
        keywords: ["stack", "stacked", "pull request", "top", "end"],
      }),
    };
  }, [selfUrl, steps, step]);
  const commands = useMemo(
    () => (active ? [moves.previous, moves.next, moves.first, moves.last] : []),
    [active, moves],
  );
  usePaletteCommands(`pull-request-stack:${instance}`, "Stack", commands);
  usePullRequestStackKeys({
    active,
    canStep: (direction) => isCommandAvailable(steps[direction].availability),
    step,
  });
  // The stack menu's Previous, Next, First and Last run ⌘K's own commands.
  return moves;
}

const STEP_ICONS = { previous: ArrowUpIcon, next: ArrowDownIcon } as const;

/**
 * Previous and Next beside the stack menu's heading: ⌘K's own commands, named as its rows are,
 * and skipped by the arrow keys where a step can't run. Next among several PRs keeps the menu
 * open on them to pick from.
 */
function StackStepItems({
  stack,
  commands,
}: {
  stack: PullRequestStackView;
  commands: Record<StackMove, PaletteCommand>;
}) {
  return (
    <div data-stack-steps="" className="flex shrink-0 items-center gap-0.5">
      {(["previous", "next"] as const).map((direction) => {
        const { label, availability, run } = commands[direction];
        const available = isCommandAvailable(availability);
        const reason = availability && !availability.available ? availability.reason : undefined;
        const Icon = STEP_ICONS[direction];
        const item = (
          <DropdownMenuItem
            key={direction}
            aria-label={reason ? `${label}: ${reason}` : label}
            title={reason ? undefined : label}
            disabled={!available}
            data-stack-step={direction}
            className="size-6 justify-center rounded-full p-0 text-muted-foreground focus:text-foreground [&_svg]:size-3.5"
            onSelect={(event) => {
              // The PRs to pick from are the menu's own rows.
              if (stack[direction].kind === "choose") event.preventDefault();
              run();
            }}
          >
            <Icon aria-hidden />
          </DropdownMenuItem>
        );
        if (!reason) return item;
        // A disabled item takes no pointer events, so the wrapper owns the hover that says why.
        // It takes no focus: the arrow keys still pass the step by.
        return (
          <KeyboardShortcutTooltip key={direction} label={reason} side="bottom">
            <span data-stack-step-reason={direction} className="inline-flex">
              {item}
            </span>
          </KeyboardShortcutTooltip>
        );
      })}
    </div>
  );
}

/** Whether `element` shows whole within the scrolling `region`. */
function withinView(region: HTMLElement, element: HTMLElement): boolean {
  const view = region.getBoundingClientRect();
  const box = element.getBoundingClientRect();
  return box.top >= view.top - 0.5 && box.bottom <= view.bottom + 0.5;
}

/**
 * Brings this PR's row into the menu's view as it opens, centred where it was out of it. Focus
 * stays where the menu put it.
 */
export function revealCurrentStackRow(region: HTMLElement | null) {
  const current = region?.querySelector<HTMLElement>('[aria-current="page"]');
  if (!region || !current || withinView(region, current)) return;
  const view = region.getBoundingClientRect();
  const box = current.getBoundingClientRect();
  region.scrollTop += box.top - view.top - (view.height - box.height) / 2;
}

/**
 * Opens the menu on this PR's row: in view, and, where the keyboard opened it onto a row, with
 * focus on this one, as a native select does. A click leaves focus on the menu.
 */
export function openOnCurrentStackRow(region: HTMLElement | null) {
  revealCurrentStackRow(region);
  const current = region?.querySelector<HTMLElement>('[aria-current="page"]');
  const focused = document.activeElement;
  // A keyboard opening puts focus on the menu's first item; a click's stays on the menu.
  const menu = region?.closest('[role="menu"]');
  if (!current || !(focused instanceof HTMLElement) || !menu?.contains(focused)) return;
  if (focused.getAttribute("role") !== "menuitem") return;
  current.focus({ preventScroll: true });
}

/** An entry in the stack menu: opened as its link says, or shown but not offered. */
/** One entry in the stack menu, in the menu's shared columns, as the Overview card shows it. */
function EntryItem({
  shown,
  open,
  current = false,
  lead,
  readAhead,
}: {
  shown: ShownEntry;
  open: (link: StackLink) => void;
  current?: boolean;
  lead: ReactNode;
  readAhead?: StackReadAhead;
}) {
  const { link, entry } = shown;
  const intent = useStackReadAheadIntent(readAhead, readAheadUrl(shown, current));
  return (
    <DropdownMenuItem
      {...intent}
      // This PR's row takes focus and arrow keys as the others do; choosing it goes nowhere.
      disabled={!link}
      aria-current={current ? "page" : undefined}
      title={current ? "This PR" : `${entry.number ?? entry.title} · ${entry.status}`}
      // Typing picks a row by its title, not by the step or tree drawn before it.
      textValue={entry.title}
      onSelect={() => link && !current && open(link)}
      className={cn(
        STACK_SPAN_CLASS,
        "items-center data-[disabled]:opacity-100",
        current && cn(CURRENT_ROW_CLASS, "cursor-default"),
      )}
    >
      <EntryColumns shown={shown} current={current} lead={lead} />
    </DropdownMenuItem>
  );
}

/** A row's place in the stack, where its tree's guides would start: its level, base first. */
function StepNumber({ step, current = false }: { step: number | null; current?: boolean }) {
  return (
    <span
      aria-hidden
      className={cn("w-4 shrink-0 text-right text-xs tabular-nums", rowDetailClass(current))}
    >
      {step}
    </span>
  );
}

const SWITCHER_CLASS =
  "inline-flex h-5 shrink-0 items-center gap-0.5 rounded-full border border-border bg-muted px-1.5 text-xs text-foreground tabular-nums";
const CHIP_CLASS =
  "inline-flex h-5 shrink-0 items-center gap-0.5 rounded-full border border-border px-1.5 text-xs text-muted-foreground tabular-nums";
const INTERACTIVE_CLASS =
  "hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none";
/** The stack menu's width: room for its titles, up to the space beside the switcher. */
const STACK_MENU_CONTENT_CLASS =
  "flex w-[min(40rem,var(--radix-dropdown-menu-content-available-width))] min-w-72 flex-col overflow-y-hidden";
const MENU_NOTE_CLASS = "shrink-0 px-1.5 pb-1 text-xs whitespace-normal text-muted-foreground";

function SwitcherFace({ stack, compact }: { stack: PullRequestStackView; compact: boolean }) {
  const { level, levels } = stack.position;
  return (
    <>
      <StackIcon />
      {!compact && level !== null && (
        <span>
          {level}
          {levels !== null && `/${levels}`}
        </span>
      )}
      <ChevronDownIcon aria-hidden className="size-3 text-muted-foreground" />
    </>
  );
}

function DependencyFace({ number }: { number: string }) {
  return (
    <>
      <ArrowUpIcon aria-hidden className="size-3" />
      depends on {number}
    </>
  );
}

/** Copies of the header's stack part at each form, for the header's fit to measure. */
export function PullRequestStackHeaderSizers({ stack }: { stack: PullRequestStackView }) {
  const dependency = shownDependency(stack);
  const chip = dependency?.entry.number && (
    <span className={CHIP_CLASS}>
      <DependencyFace number={dependency.entry.number} />
    </span>
  );
  const switcher = (
    <span className={SWITCHER_CLASS}>
      <SwitcherFace stack={stack} compact={false} />
    </span>
  );
  return (
    <>
      <span data-header-stack="full" className="inline-flex gap-1">
        {switcher}
        {chip}
      </span>
      <span data-header-stack="compact" className={SWITCHER_CLASS}>
        <SwitcherFace stack={stack} compact />
      </span>
    </>
  );
}

/**
 * Which PR's stack menu is open and how its Overview card is folded, kept above the header and
 * details, so a menu stays open and an opened fold stays open as later reads swap them.
 */
interface StackScopeValue {
  openFor: string | null;
  setOpenFor: (url: string | null) => void;
  /** The fold the person left the selected PR's card in, or null while it's as it starts. */
  fold: { url: string; state: StackFoldState } | null;
  setFold: (url: string, state: StackFoldState) => void;
}

const StackScopeContext = createContext<StackScopeValue | null>(null);

interface StackScopeState {
  selected: string | null;
  menuFor: string | null;
  fold: { url: string; state: StackFoldState } | null;
}

/** Holds the stack menu and card's state for the PR shown, `selectedUrl`, across its swaps. */
export function PullRequestStackScope({
  selectedUrl,
  children,
}: {
  selectedUrl: string | null;
  children: ReactNode;
}) {
  const [state, setState] = useState<StackScopeState>({
    selected: selectedUrl,
    menuFor: null,
    fold: null,
  });
  // Another PR shown starts with its menu closed and its card folded, and so does coming back.
  if (state.selected !== selectedUrl)
    setState({ selected: selectedUrl, menuFor: null, fold: null });
  const current = state.selected === selectedUrl ? state : null;
  const openFor = current?.menuFor ?? null;
  const fold = current?.fold ?? null;
  const value = useMemo<StackScopeValue>(
    () => ({
      openFor,
      setOpenFor: (url) =>
        setState((previous) => ({ ...previous, selected: selectedUrl, menuFor: url })),
      fold,
      setFold: (url, foldState) =>
        setState((previous) => ({
          ...previous,
          selected: selectedUrl,
          fold: { url, state: foldState },
        })),
    }),
    [openFor, fold, selectedUrl],
  );
  return <StackScopeContext.Provider value={value}>{children}</StackScopeContext.Provider>;
}

/** The menu's open state: the scope's, where one holds it, else its own. */
function useStackMenuOpen(selfUrl: string | null): [boolean, (open: boolean) => void] {
  const scope = useContext(StackScopeContext);
  const [ownOpen, setOwnOpen] = useState(false);
  const setScoped = scope?.setOpenFor;
  const setOpen = useCallback(
    (open: boolean) => (setScoped ? setScoped(open ? selfUrl : null) : setOwnOpen(open)),
    [setScoped, selfUrl],
  );
  return [scope ? selfUrl !== null && scope.openFor === selfUrl : ownOpen, setOpen];
}

/** The card's fold for the PR at `selfUrl`: the scope's, where one holds it, else its own. */
function useStackFold(selfUrl: string | null): [StackFoldState, (state: StackFoldState) => void] {
  const scope = useContext(StackScopeContext);
  const [ownFold, setOwnFold] = useState<StackFoldState>(STACK_FOLDED);
  const setScoped = scope?.setFold;
  const setFold = useCallback(
    (state: StackFoldState) => {
      if (setScoped && selfUrl !== null) setScoped(selfUrl, state);
      else setOwnFold(state);
    },
    [setScoped, selfUrl],
  );
  if (!scope || selfUrl === null) return [ownFold, setFold];
  return [scope.fold?.url === selfUrl ? scope.fold.state : STACK_FOLDED, setFold];
}

/** The header's `🥞 2/3` switcher: the whole stack, its dependency, and Previous and Next. */
export function PullRequestStackMenu({
  stack,
  active,
  compact = false,
}: {
  stack: PullRequestStackView;
  active: boolean;
  /** Just the pancake, for a narrow header; the dependency stays in the menu. */
  compact?: boolean;
}) {
  const [menuOpen, setMenuOpen] = useStackMenuOpen(
    stack.parse.nodes[stack.parse.selfIndex]?.ref?.url ?? null,
  );
  const open = useOpenInStack(stack);
  const commands = usePullRequestStackCommands(stack, active, open, () => setMenuOpen(true));
  const rowsRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!menuOpen) return;
    // After the menu's own focus lands, and once it's placed with its height.
    const frame = requestAnimationFrame(() => openOnCurrentStackRow(rowsRef.current));
    return () => cancelAnimationFrame(frame);
  }, [menuOpen]);
  const name = stackPositionName(stack.position);
  const dependency = shownDependency(stack);
  const straight = isStraightStack(stack.parse.nodes);
  return (
    <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={name}
          title={name}
          data-pull-request-stack-menu=""
          className={cn(SWITCHER_CLASS, INTERACTIVE_CLASS)}
        >
          <SwitcherFace stack={stack} compact={compact} />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        // Clear of the window's edge, so a long stack's menu doesn't end flush with it.
        collisionPadding={8}
        className={STACK_MENU_CONTENT_CLASS}
      >
        <div data-stack-menu-heading="" className="flex shrink-0 items-center gap-2">
          <DropdownMenuLabel className="flex min-w-0 flex-1 flex-col text-xs font-normal text-muted-foreground">
            <StackSummary stack={stack} />
          </DropdownMenuLabel>
          <StackStepItems stack={stack} commands={commands} />
        </div>
        {stack.parse.trust === "tolerated" && (
          <p className={MENU_NOTE_CLASS}>{STACK_OUT_OF_DATE}</p>
        )}
        {stack.partial && !stack.completing && (
          <>
            <p className={MENU_NOTE_CLASS}>{STACK_PARTIAL}</p>
            <DropdownMenuItem
              className="shrink-0"
              onSelect={() => open({ url: stack.partial!.url, external: true })}
            >
              <ExternalLinkIcon aria-hidden />
              {SEE_WHOLE_STACK}
            </DropdownMenuItem>
            <DropdownMenuSeparator className="shrink-0" />
          </>
        )}
        {/* Only the rows scroll: the summary above and the keys below stay in view. */}
        <div ref={rowsRef} data-stack-menu-rows="" className="min-h-0 overflow-y-auto">
          {/* The card's columns: every row's number and status line up, the dependency's too. */}
          <DropdownMenuGroup className={cn("@container", STACK_COLUMNS_CLASS)}>
            {dependency && (
              <>
                <DropdownMenuLabel className="col-span-full pb-0 text-xs font-normal text-muted-foreground">
                  Depends on
                </DropdownMenuLabel>
                <EntryItem
                  shown={dependency}
                  open={open}
                  readAhead={stack.readAhead}
                  lead={<ArrowUpIcon aria-hidden className="size-3.5 w-4 text-muted-foreground" />}
                />
                <DropdownMenuSeparator className="col-span-full" />
              </>
            )}
            {rowEntries(stack).map(({ row, shown, current }) => (
              <EntryItem
                key={row.index}
                shown={shown}
                open={open}
                current={current}
                readAhead={stack.readAhead}
                lead={
                  <>
                    <StepNumber step={stack.rowNumbers[row.index] ?? null} current={current} />
                    {/* A straight stack needs no tree: its steps say the order. */}
                    {!straight && <TreeGuides row={row} />}
                  </>
                }
              />
            ))}
            {stack.completing && <StackLoadingRest className="col-span-full px-2 py-1.5" />}
          </DropdownMenuGroup>
        </div>
        {/* A long stack's ends, as ⌘K offers them, stay in view with the keys below the rows. */}
        {stack.rows.length > FOLD_ABOVE_ROWS && (
          <>
            <DropdownMenuSeparator className="shrink-0" />
            {[commands.first, commands.last].map((command) => {
              const Icon = command.icon;
              const { availability } = command;
              const available = isCommandAvailable(availability);
              return (
                <DropdownMenuItem
                  key={command.id}
                  disabled={!available}
                  title={availability && !availability.available ? availability.reason : undefined}
                  className="shrink-0"
                  onSelect={command.run}
                >
                  <Icon aria-hidden />
                  {command.label}
                </DropdownMenuItem>
              );
            })}
          </>
        )}
        <DropdownMenuSeparator className="shrink-0" />
        <StackKeysHint />
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** Under the stack, the keys that move down and up it, read from the shortcut registry. */
function StackKeysHint() {
  const next = shortcutKeys(STACK_SHORTCUTS.next);
  const previous = shortcutKeys(STACK_SHORTCUTS.previous);
  if (!next || !previous) return null;
  return (
    <p
      data-stack-keys-hint=""
      className="flex shrink-0 items-center gap-3 px-2 py-1.5 text-xs text-muted-foreground"
    >
      <span className="flex items-center gap-1">
        <CompactShortcutKeys keys={next} /> Next
      </span>
      <span className="flex items-center gap-1">
        <CompactShortcutKeys keys={previous} /> Previous
      </span>
    </p>
  );
}

/** `⬆ depends on #99`: a PR outside the stack that the description says it depends on. */
export function PullRequestStackDependency({ stack }: { stack: PullRequestStackView }) {
  const open = useOpenInStack(stack);
  const dependency = shownDependency(stack);
  const link = dependency?.link;
  const number = dependency?.entry.number;
  if (!dependency || !link || !number) return null;
  const { entry } = dependency;
  const title = `Depends on ${number}: ${entry.title} · ${entry.status}`;
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      onClick={() => open(link)}
      className={cn(CHIP_CLASS, INTERACTIVE_CLASS)}
    >
      <DependencyFace number={number} />
    </button>
  );
}

const plainClick = (event: MouseEvent) =>
  event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;

/**
 * A card row as a link to its PR: a plain click opens it as the stack opens entries, while a
 * modified or middle click and Copy Link get the link itself.
 */
function EntryLink({
  shown,
  open,
  className,
  children,
  readAhead,
}: {
  shown: ShownEntry & { link: StackLink };
  open: (link: StackLink) => void;
  className: string;
  children: ReactNode;
  readAhead?: StackReadAhead;
}) {
  const { link, entry } = shown;
  const intent = useStackReadAheadIntent(readAhead, readAheadUrl(shown, false));
  return (
    <a
      {...intent}
      href={link.url}
      target="_blank"
      rel="noreferrer"
      title={`Open ${entry.number ?? ""} ${entry.title}`}
      onClick={(event) => {
        if (event.defaultPrevented || !plainClick(event)) return;
        event.preventDefault();
        open(link);
      }}
      className={cn(className, INTERACTIVE_CLASS)}
    >
      {children}
    </a>
  );
}

// Each row spans the card's columns, so its number and status sit in the same tracks as every
// other row's: the number and status are as wide as their widest, the title takes the rest.
// The number and status each take at most a share of the card, truncating past it with their
// full text on hover, so one long reference or status can't take every row's title; and the
// title keeps a floor whatever they need.
const STACK_COLUMNS_CLASS =
  "grid grid-cols-[minmax(min(11rem,50%),1fr)_fit-content(min(6rem,18cqw))_fit-content(min(8rem,24cqw))] gap-x-2";
const STACK_SPAN_CLASS = "col-span-full grid grid-cols-subgrid";
const CARD_ROW_CLASS = cn(STACK_SPAN_CLASS, "w-full min-w-0 items-center px-2.5 py-1 text-left");

function CardRow({
  shown,
  open,
  current = false,
  children,
  readAhead,
  rowKey,
}: {
  shown: ShownEntry;
  open: (link: StackLink) => void;
  current?: boolean;
  children: ReactNode;
  readAhead?: StackReadAhead;
  /** The row's {@link stackRowKey}, where a folded card may move focus to it. */
  rowKey?: string;
}) {
  const { link } = shown;
  return (
    <li className={STACK_SPAN_CLASS} data-stack-row-key={rowKey}>
      {current || !link ? (
        <div
          // Focus can land here when a gap it was folded into opens.
          tabIndex={rowKey === undefined ? undefined : -1}
          className={cn(
            CARD_ROW_CLASS,
            "focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
            current && CURRENT_ROW_CLASS,
          )}
          aria-current={current ? "page" : undefined}
        >
          {children}
        </div>
      ) : (
        <EntryLink
          shown={{ ...shown, link }}
          open={open}
          className={CARD_ROW_CLASS}
          readAhead={readAhead}
        >
          {children}
        </EntryLink>
      )}
    </li>
  );
}

type CardEntry = ReturnType<typeof rowEntries>[number];

/** How a gap names a row beside it: its PR's number, else its step, else its branch. */
function gapNeighborName(stack: PullRequestStackView, entry: CardEntry): string {
  if (entry.shown.entry.number) return entry.shown.entry.number;
  const step = stack.rowNumbers[entry.row.index];
  return step === null || step === undefined ? entry.row.node.branch : `step ${step}`;
}

/** Rows folded away between two shown ones: `⋯ 6 more`, which shows them. */
function StackGapRow({
  stack,
  hidden,
  before,
  after,
  tree,
  onReveal,
}: {
  stack: PullRequestStackView;
  hidden: number;
  before: CardEntry;
  after: CardEntry;
  /** Whether the card draws the stack's tree, which the gap's lines carry on. */
  tree: boolean;
  onReveal: () => void;
}) {
  const what = hidden === 1 ? "pull request" : "pull requests";
  const name = `Show ${hidden} more ${what} between ${gapNeighborName(stack, before)} and ${gapNeighborName(stack, after)}`;
  return (
    <li className={STACK_SPAN_CLASS}>
      <button
        type="button"
        aria-label={name}
        title={name}
        onClick={onReveal}
        className={cn(CARD_ROW_CLASS, INTERACTIVE_CLASS, "text-xs text-muted-foreground")}
      >
        <span className="col-span-full flex min-w-0 items-center gap-1.5">
          <span aria-hidden className="w-4 shrink-0" />
          {/* The lines that carry on past the folded rows, so the tree still reads. */}
          {tree && (
            <span aria-hidden className="flex shrink-0 font-mono text-foreground-tertiary">
              {after.row.guides
                .map((carries, level) => ({ carries, level }))
                .map((guide) => (
                  <span key={guide.level} className="w-3 text-center">
                    {guide.carries ? "│" : ""}
                  </span>
                ))}
              {after.row.connector !== null && <span className="w-3 text-center">┆</span>}
            </span>
          )}
          <span aria-hidden>⋯ {hidden} more</span>
        </span>
      </button>
    </li>
  );
}

/** The Overview's stack card, shown in place of the description's stack section. */
export function PullRequestStackCard({ stack }: { stack: PullRequestStackView }) {
  const open = useOpenInStack(stack);
  const dependency = shownDependency(stack);
  const [fold, setFold] = useStackFold(stack.parse.nodes[stack.parse.selfIndex]?.ref?.url ?? null);
  const entries = rowEntries(stack);
  const segments = foldStackRows(
    entries,
    entries.findIndex((entry) => entry.current),
    (entry) => stackRowKey(entry.row.node),
    fold,
  );
  const folds = entries.length > FOLD_ABOVE_ROWS;
  const folded = segments.some((segment) => segment.kind === "gap");
  // A straight stack needs no tree: its steps say the order, as in the menu.
  const tree = !isStraightStack(stack.parse.nodes);
  // A gap's button goes as it opens: focus moves to the first row it showed.
  const list = useRef<HTMLOListElement>(null);
  const focusKey = useRef<string | null>(null);
  useLayoutEffect(() => {
    const key = focusKey.current;
    if (key === null) return;
    focusKey.current = null;
    const row = [...(list.current?.children ?? [])].find(
      (item) => item instanceof HTMLElement && item.dataset.stackRowKey === key,
    );
    row?.querySelector<HTMLElement>(":scope > :is(a, div)")?.focus();
  });
  return (
    <nav
      aria-label="Stack"
      data-pull-request-stack-card=""
      className="@container my-2 rounded-md border border-border text-ui"
    >
      <div className="flex min-w-0 items-center gap-x-1.5 border-b border-border px-2.5 py-1.5 text-xs whitespace-nowrap text-muted-foreground">
        <StackIcon />
        <StackSummary stack={stack} inline />
        {folds && (
          <button
            type="button"
            aria-expanded={!folded}
            aria-label={folded ? "Show the whole stack" : "Fold the stack around this PR"}
            onClick={() => setFold(folded ? { ...STACK_FOLDED, expandedAll: true } : STACK_FOLDED)}
            className="ml-auto shrink-0 rounded-sm px-1 text-foreground underline-offset-2 hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          >
            {folded ? "Expand all" : "Collapse"}
          </button>
        )}
      </div>
      {stack.partial && !stack.completing && (
        <p className="flex flex-wrap items-center gap-x-1.5 border-b border-border px-2.5 py-1.5 text-xs text-muted-foreground">
          {STACK_PARTIAL}
          <button
            type="button"
            className="inline-flex items-center gap-1 text-foreground underline-offset-2 hover:underline focus-visible:underline"
            onClick={() => open({ url: stack.partial!.url, external: true })}
          >
            {SEE_WHOLE_STACK}
            <ExternalLinkIcon aria-hidden className="size-3" />
          </button>
        </p>
      )}
      <div className={STACK_COLUMNS_CLASS}>
        {dependency && (
          <div className={cn(STACK_SPAN_CLASS, "border-b border-border py-1")}>
            <p className="col-span-full px-2.5 text-xs text-muted-foreground">Depends on</p>
            <ul className={STACK_SPAN_CLASS}>
              <CardRow shown={dependency} open={open} readAhead={stack.readAhead}>
                <EntryColumns
                  shown={dependency}
                  lead={
                    <ArrowUpIcon aria-hidden className="size-3.5 shrink-0 text-muted-foreground" />
                  }
                />
              </CardRow>
            </ul>
          </div>
        )}
        <ol ref={list} className={cn(STACK_SPAN_CLASS, "py-1")}>
          {segments.map((segment) =>
            segment.kind === "gap" ? (
              <StackGapRow
                key={`gap:${segment.keys[0]}`}
                stack={stack}
                hidden={segment.rows.length}
                before={segment.before}
                after={segment.after}
                tree={tree}
                onReveal={() => {
                  focusKey.current = segment.keys[0] ?? null;
                  setFold(revealStackRows(fold, segment.keys));
                }}
              />
            ) : (
              <CardRow
                key={segment.row.row.index}
                shown={segment.row.shown}
                open={open}
                current={segment.row.current}
                readAhead={stack.readAhead}
                rowKey={stackRowKey(segment.row.row.node)}
              >
                <EntryColumns
                  shown={segment.row.shown}
                  current={segment.row.current}
                  lead={
                    <>
                      <StepNumber
                        step={stack.rowNumbers[segment.row.row.index] ?? null}
                        current={segment.row.current}
                      />
                      {tree && <TreeGuides row={segment.row.row} />}
                    </>
                  }
                />
              </CardRow>
            ),
          )}
        </ol>
        {stack.completing && <StackLoadingRest className="col-span-full px-2.5 pb-1.5" />}
      </div>
    </nav>
  );
}

/** Below a stack shown in part, while the PR's own read may still list the rest of it. */
function StackLoadingRest({ className }: { className?: string }) {
  return (
    <p
      role="status"
      data-stack-loading-rest=""
      className={cn("flex items-center gap-1.5 text-xs text-muted-foreground", className)}
    >
      <Spinner aria-hidden className="size-3" />
      {STACK_LOADING_REST}
    </p>
  );
}

/** The header's switcher while its stack reads: its shape, so the header doesn't shift. */
export function PullRequestStackPending() {
  return (
    <span
      role="status"
      aria-label={STACK_LOADING}
      title={STACK_LOADING}
      data-pull-request-stack-pending=""
      className={SWITCHER_CLASS}
    >
      <StackIcon />
      <Spinner aria-hidden className="size-3 text-muted-foreground" />
    </span>
  );
}
