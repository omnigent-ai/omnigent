// Revealing the Settings row a URL names: scroll it to the center, focus its control and
// briefly mark the row itself. Each row reveals itself when it mounts, so a row that
// appears late (a lazy section, data that loads) is still found.

import { computeAccessibleName } from "dom-accessibility-api";

import { settingsEntryById } from "./catalog";
import { type SettingsEntry, settingsChoiceNamed, settingsRevealer } from "./catalog/types";
import { type SettingsRevealTarget, settingsControlId } from "./settingsControl";

/** A row mounting later than this after the navigation no longer moves the page. */
export const SETTINGS_REVEAL_WINDOW_MS = 3000;
/** How long the revealed row stays marked. */
export const SETTINGS_HIGHLIGHT_MS = 1500;
/** How long a revealer waits for the row it stands in for before revealing itself. */
export const SETTINGS_REVEALER_GRACE_MS = 300;

export const SETTINGS_HIGHLIGHT_ATTRIBUTE = "data-settings-highlight";

/**
 * How a revealed row is marked: a tint that reaches a little past the row without moving
 * it, shown at once and faded out, or switched off without motion where motion is reduced.
 */
export const SETTINGS_HIGHLIGHT_CLASS =
  "rounded-lg transition-[background-color,box-shadow] duration-700 ease-out motion-reduce:transition-none data-[settings-highlight]:bg-accent data-[settings-highlight]:shadow-[0_0_0_8px_var(--color-accent)] data-[settings-highlight]:duration-150";

const mountedAnchors = new Map<string, number>();

/** The navigation on screen and when it happened; Back and Forward start a new one. */
let currentNavigation: { location: object; startedAt: number } | undefined;
/** A revealer standing in for a row that hasn't appeared, until that row does. */
let activeFallback: { location: object; controller: AbortController } | undefined;

/**
 * Starts the reveal window of the navigation to `location`. The Settings page calls this on
 * every location change, so each navigation, including Back and Forward, gets its own window,
 * shared by every row that mounts during it.
 */
export function noteSettingsNavigation(location: object, now: number = Date.now()): void {
  if (currentNavigation?.location !== location) currentNavigation = { location, startedAt: now };
}

/** When the reveal window of `location` ends; a row that renders before its navigation is noted gets a full one. */
function revealWindowEndsAt(location: object, now: number): number {
  if (currentNavigation?.location !== location) return now + SETTINGS_REVEAL_WINDOW_MS;
  return currentNavigation.startedAt + SETTINGS_REVEAL_WINDOW_MS;
}

/** Whether a row for `location` may still move the page. */
function withinRevealWindow(location: object, now: number): boolean {
  // A row that renders with its navigation runs before the page notes it, so it's on time.
  if (currentNavigation?.location !== location) return true;
  return now - currentNavigation.startedAt <= SETTINGS_REVEAL_WINDOW_MS;
}

/** Records that a row for `id` is on screen, until the returned cleanup runs. */
export function registerSettingsAnchor(id: string): () => void {
  mountedAnchors.set(id, (mountedAnchors.get(id) ?? 0) + 1);
  return () => {
    const count = (mountedAnchors.get(id) ?? 1) - 1;
    if (count > 0) mountedAnchors.set(id, count);
    else mountedAnchors.delete(id);
  };
}

const OWNED_CONTROLS =
  "[data-settings-control], [data-settings-widget] button, [data-settings-widget] [role='switch']";

/** What hides a control from focus, on it or an ancestor. */
const HIDDEN_FROM_FOCUS = "[hidden], [aria-hidden='true'], [inert]";

/** The row's own controls, as shown: not another row's, not hidden, and not its help button. */
function ownedControls(row: HTMLElement): HTMLElement[] {
  return [...row.querySelectorAll<HTMLElement>(OWNED_CONTROLS)].filter(
    (control) =>
      control.closest("[data-settings-item]") === row && !control.closest(HIDDEN_FROM_FOCUS),
  );
}

/** The control a part (or `""`, the main control) names, as the row shows it. */
function partControl(row: HTMLElement, entry: SettingsEntry, part: string): HTMLElement | null {
  const id = settingsControlId(entry, part);
  return (
    ownedControls(row).find(
      (control) =>
        control.getAttribute("data-settings-control") === id ||
        control.closest("[data-settings-widget]")?.getAttribute("data-settings-widget") === id,
    ) ?? null
  );
}

/**
 * What a reveal may focus, best first. The row (no part) and its main control (`""`) each name
 * one control and wait for it. A part that isn't usable right now, such as Save before an edit
 * or Retry while nothing failed, falls back to the main control and then the row's others
 * until it is.
 */
function focusCandidates(row: HTMLElement, entry: SettingsEntry, part?: string): HTMLElement[] {
  return focusableCandidates(row, entry, part)
    .map((control) => focusTarget(control))
    .filter((control): control is HTMLElement => control !== null);
}

function focusableCandidates(row: HTMLElement, entry: SettingsEntry, part?: string): HTMLElement[] {
  if (part === undefined) {
    const named = entry.focus === undefined ? null : partControl(row, entry, entry.focus);
    const first = named ?? ownedControls(row)[0] ?? null;
    return first ? [first] : [];
  }
  const main = partControl(row, entry, "");
  if (part === "") return main ? [main] : [];
  const own = partControl(row, entry, part);
  return [...new Set([own, main, ...ownedControls(row)].filter((control) => control !== null))];
}

/**
 * What takes focus for a control: itself; for a radio group, which can't take focus, its own
 * checked radio, else its first enabled one; for a tab list, its own selected tab only, because
 * focusing another tab would select it. Nothing is selected or opened.
 */
function focusTarget(control: HTMLElement): HTMLElement | null {
  if (control.getAttribute("role") === "tablist") return selectedTab(control);
  if (control.getAttribute("role") !== "radiogroup") return control;
  const row = control.closest("[data-settings-item]");
  // ARIA radios and native ones alike; a native radio's checked state is its property.
  const radios = [
    ...control.querySelectorAll<HTMLElement>('[role="radio"], input[type="radio"]'),
  ].filter(
    (radio) =>
      radio.closest('[role="radiogroup"]') === control &&
      radio.closest("[data-settings-item]") === row &&
      !radio.closest(HIDDEN_FROM_FOCUS) &&
      !radio.matches(":disabled") &&
      radio.getAttribute("aria-disabled") !== "true",
  );
  const checked = (radio: HTMLElement) =>
    radio instanceof HTMLInputElement
      ? radio.checked
      : radio.getAttribute("aria-checked") === "true";
  return radios.find(checked) ?? radios[0] ?? null;
}

/** A tab list part's static choice, as a revealer names it. */
interface TabChoice {
  part: string;
  name: string;
}

/**
 * The tab of `choice` in the row's tab list part: its selected tab, when that's the choice,
 * named statically or around a count. Another tab would be selected by focusing it, so it waits.
 */
function choiceTab(row: HTMLElement, entry: SettingsEntry, choice: TabChoice): HTMLElement | null {
  const list = partControl(row, entry, choice.part);
  if (list?.getAttribute("role") !== "tablist") return null;
  const tab = selectedTab(list);
  // Named exactly as the audit names it, so a tab it accepts is one reveal finds.
  return tab && settingsChoiceNamed(entry, choice.part, choice.name, computeAccessibleName(tab))
    ? tab
    : null;
}

/** A tab list's selected tab, when it's this list's own, in its row, and can take focus. */
function selectedTab(list: HTMLElement): HTMLElement | null {
  const row = list.closest("[data-settings-item]");
  return (
    [...list.querySelectorAll<HTMLElement>('[role="tab"][aria-selected="true"]')].find(
      (tab) =>
        tab.parentElement?.closest('[role="tablist"]') === list &&
        tab.closest("[data-settings-item]") === row &&
        !tab.closest(HIDDEN_FROM_FOCUS) &&
        !tab.matches(":disabled") &&
        tab.getAttribute("aria-disabled") !== "true",
    ) ?? null
  );
}

/**
 * Focuses the row's control now, or once it exists and is enabled, while the reveal window is
 * open. A part that isn't usable yet lands on a stand-in at once, then takes focus over from it
 * when it appears, as one shown once data loads does, unless the user has acted in the meantime.
 * Waiting stops when the navigation is aborted, when focus moves somewhere else after the
 * navigation began, or when the window closes.
 */
function focusControlWhenReady(
  row: HTMLElement,
  entry: SettingsEntry,
  part: string | undefined,
  signal: AbortSignal,
  windowEndsAt: number,
  choice?: TabChoice,
) {
  const focusedBeforeNavigation = document.activeElement;
  /** The control focused in place of the part asked for, while that part may still appear. */
  let standIn: HTMLElement | null = null;
  /** Whether focus now rests on the control asked for, so the wait is over. */
  const focusIfReady = (): boolean => {
    const candidates = choice
      ? [choiceTab(row, entry, choice)].filter((tab): tab is HTMLElement => tab !== null)
      : focusCandidates(row, entry, part);
    const control = candidates.find((each) => !each.matches(":disabled"));
    if (!control) return false;
    if (control === standIn && document.activeElement === standIn) return false;
    control.focus({ preventScroll: true });
    if (document.activeElement !== control) return false;
    const wanted = candidates[0] === control && isAskedFor(row, entry, part, control);
    standIn = wanted ? null : control;
    return wanted;
  };
  if (focusIfReady()) return;
  const observer = new MutationObserver(() => {
    const active = document.activeElement;
    // Focus that falls to the page, as when the stand-in unmounts, isn't a move.
    const movedAway = standIn
      ? active !== standIn && active !== document.body
      : active !== focusedBeforeNavigation && active !== document.body && !row.contains(active);
    if (movedAway || focusIfReady()) stop();
  });
  const timer = window.setTimeout(stop, Math.max(0, windowEndsAt - Date.now()));
  // Pressing a key or the pointer while the reveal waits makes the focus the user's own.
  const userActed = () => stop();
  function stop() {
    observer.disconnect();
    window.clearTimeout(timer);
    signal.removeEventListener("abort", stop);
    document.removeEventListener("keydown", userActed, true);
    document.removeEventListener("pointerdown", userActed, true);
  }
  document.addEventListener("keydown", userActed, true);
  document.addEventListener("pointerdown", userActed, true);
  observer.observe(row, {
    subtree: true,
    childList: true,
    attributes: true,
    // Whatever decides whether a control can take focus: disabled, or hidden from it; and for a
    // tab list, which of its tabs is selected.
    attributeFilter: [
      "disabled",
      "aria-disabled",
      "hidden",
      "aria-hidden",
      "inert",
      "aria-selected",
    ],
  });
  // A tab choice is ready once its tab's name is, which a label anywhere on the page can set:
  // by its text, its id, its own label, or what's hidden in it.
  if (choice) {
    observer.observe(document.body, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
      // Hiding by attribute is watched below, across the whole document.
      attributeFilter: ["id", "aria-label", "aria-labelledby", "style", "class"],
    });
  }
  // An ancestor outside the row can hide it too, as a closed drawer or dialog does.
  observer.observe(document.documentElement, {
    subtree: true,
    attributes: true,
    attributeFilter: ["hidden", "aria-hidden", "inert"],
  });
  signal.addEventListener("abort", stop);
}

/** Whether `control` is the one the reveal names, rather than a stand-in for a missing part. */
function isAskedFor(
  row: HTMLElement,
  entry: SettingsEntry,
  part: string | undefined,
  control: HTMLElement,
): boolean {
  if (part === undefined || part === "") return true;
  const own = partControl(row, entry, part);
  return own !== null && focusTarget(own) === control;
}

function revealRow(
  row: HTMLElement,
  entry: SettingsEntry,
  part: string | undefined,
  signal: AbortSignal,
  windowEndsAt: number,
  choice?: TabChoice,
) {
  row.scrollIntoView?.({ block: "center" });
  focusControlWhenReady(row, entry, part, signal, windowEndsAt, choice);
  // Re-adding the mark restarts its fade when the same row is chosen again.
  row.removeAttribute(SETTINGS_HIGHLIGHT_ATTRIBUTE);
  void row.offsetWidth;
  row.setAttribute(SETTINGS_HIGHLIGHT_ATTRIBUTE, "");
  const clear = () => row.removeAttribute(SETTINGS_HIGHLIGHT_ATTRIBUTE);
  const timer = window.setTimeout(clear, SETTINGS_HIGHLIGHT_MS);
  signal.addEventListener("abort", () => {
    window.clearTimeout(timer);
    clear();
  });
}

export interface SettingsRevealRequest {
  row: HTMLElement;
  entry: SettingsEntry;
  target: SettingsRevealTarget;
  /** The navigation's location object: each navigation, even to the same hash, reveals again. */
  location: object;
  signal: AbortSignal;
}

/**
 * Reveals `row` when the target names it, or when it is the target's `revealedBy` and the
 * target hasn't appeared. Aborting `signal` (a newer navigation, unmount) cancels all of it,
 * and the target appearing cancels its revealer's stand-in.
 */
export function revealSettingsRow({
  row,
  entry,
  target,
  location,
  signal,
}: SettingsRevealRequest): void {
  if (!withinRevealWindow(location, Date.now())) return;
  const windowEndsAt = revealWindowEndsAt(location, Date.now());
  if (target.id === entry.id) {
    if (activeFallback?.location === location) activeFallback.controller.abort();
    revealRow(row, entry, target.part, signal, windowEndsAt);
    return;
  }
  const targetEntry = settingsEntryById(target.id);
  const revealer = targetEntry ? settingsRevealer(targetEntry) : undefined;
  if (revealer?.id !== entry.id) return;
  const fallback = new AbortController();
  signal.addEventListener("abort", () => fallback.abort());
  // Focus the user moves while the hidden row has its chance to appear is theirs to keep. Focus
  // that falls to the page, as when a page the navigation closes unmounts, isn't a move.
  const focusedAtStart = document.activeElement;
  const timer = window.setTimeout(() => {
    if (mountedAnchors.has(target.id) || !withinRevealWindow(location, Date.now())) return;
    const active = document.activeElement;
    if (active !== focusedAtStart && active !== null && active !== document.body) return;
    activeFallback = { location, controller: fallback };
    // A revealer named by its tab list's choice reveals that tab, once it's the selected one.
    const choice =
      revealer.part === undefined ? undefined : { part: revealer.part, name: revealer.choice! };
    revealRow(row, entry, revealer.part, fallback.signal, windowEndsAt, choice);
  }, SETTINGS_REVEALER_GRACE_MS);
  fallback.signal.addEventListener("abort", () => {
    window.clearTimeout(timer);
    if (activeFallback?.controller === fallback) activeFallback = undefined;
  });
}
