// A session's browser tab records while the advanced browser
// tools are on. The desktop main process holds them
// (electron/src/browserTabs.js), so every window agrees on them and
// changes are compare-and-set rather than read-modify-writes of shared
// localStorage; the saved workspace keeps a copy for the next launch. The
// local tab store (hooks/useBrowserTabs.ts) reads and writes through
// here, and keeps its own storage while the tools are
// off. Imports nothing from that store.
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { AGENT_ACTION_EXPIRED_ERROR } from "./deadline";
import { browserAgentToolsActive } from "./preferences";

/** The reserved agent tab's id (AGENT_BROWSER_TAB_ID in hooks/useBrowserTabs.ts). */
export const RESERVED_TAB_ID = "agent-browser";

/** A session's tabs as main holds them. */
export interface TabsRecord {
  /** Main's count of changes; 0 for a session main has no record of yet. */
  version: number;
  tabs: string[];
  selected: string | null;
  /** The tab agents act on, while it's open (null: the reserved tab). */
  agentTab: string | null;
  /** Agent and link navigations per open tab, so a close begun before one keeps the tab. */
  navs: Record<string, number>;
}

type TabsChange = Omit<TabsRecord, "version">;

interface UpdateAnswer {
  ok: boolean;
  record?: TabsRecord | null;
  conflict?: boolean;
  expired?: boolean;
  error?: string;
}

interface TabsBridge {
  browserTabs?: (args: Record<string, unknown>) => Promise<UpdateAnswer>;
  onBrowserTabsChanged?: (
    callback: (payload: { conversationId?: string; record?: TabsRecord; reset?: true }) => void,
  ) => () => void;
}

/** Thrown by `changeTabs` when its deadline passes before the change is made. */
export class BrowserTabsExpired extends Error {
  constructor() {
    super(AGENT_ACTION_EXPIRED_ERROR);
    this.name = "BrowserTabsExpired";
  }
}

function bridge(): TabsBridge | undefined {
  return (window as { omnigentDesktop?: TabsBridge }).omnigentDesktop;
}

// The newest record main has sent for each session.
const records = new Map<string, TabsRecord>();
// Changes this window made that main hasn't answered yet, newest last.
const pending = new Map<string, TabsChange>();
const listeners = new Set<(conversationId: string) => void>();
// The bridge this window listens to (one per app run; tests swap it).
let subscribed: { to: TabsBridge | undefined; stop: () => void } | null = null;

function notify(conversationId: string): void {
  for (const listener of listeners) listener(conversationId);
}

/** Hold `record` unless a newer one is held; true when it was taken. */
function keep(conversationId: string, record: TabsRecord): boolean {
  const known = records.get(conversationId);
  if (known && known.version > record.version) return false;
  records.set(conversationId, record);
  return true;
}

/** Save a newly held record for the next launch, so a late older answer never wins. */
function persist(conversationId: string, record: TabsRecord): void {
  writeSessionWorkspaceState(conversationId, {
    openBrowsers: record.tabs,
    selectedBrowserId: record.selected,
  });
}

/** Where tabs go back to when the switch goes off: the local store's
 *  save, which keeps them in memory if storage refuses them (it registers
 *  itself, as this module imports nothing from it). */
type HandBack = (conversationId: string, tabs: { tabs: string[]; selected: string | null }) => void;
let handBack: HandBack = (conversationId, { tabs, selected }) => {
  writeSessionWorkspaceState(conversationId, { openBrowsers: tabs, selectedBrowserId: selected });
};

/** Set where the tabs go when the switch goes off (see HandBack). */
export function handTabsBackTo(save: HandBack): void {
  handBack = save;
}

/** Give every session's tabs, as shown, back to the local store, so a
 *  tab whose save failed (storage full) stays in its rail. */
function handBackAll(): void {
  for (const conversationId of new Set([...records.keys(), ...pending.keys()])) {
    const shown = pending.get(conversationId) ?? records.get(conversationId);
    if (shown) handBack(conversationId, { tabs: shown.tabs, selected: shown.selected });
  }
}

/** Listen to main once per window: other windows' changes, and resets. */
function subscribe(): void {
  const to = bridge();
  if (subscribed?.to === to) return;
  subscribed?.stop();
  records.clear();
  pending.clear();
  const stop = to?.onBrowserTabsChanged?.((payload) => {
    if (payload.reset) {
      // The switch went off: hooks showing a session re-read its saved tabs.
      const shown = [...records.keys()];
      handBackAll();
      records.clear();
      pending.clear();
      for (const conversationId of shown) notify(conversationId);
      return;
    }
    if (!payload.conversationId || !payload.record) return;
    keep(payload.conversationId, payload.record);
    notify(payload.conversationId);
  });
  subscribed = { to, stop: stop ?? (() => {}) };
}

/** The record main would start from: the saved workspace's tabs. */
function fromWorkspace(conversationId: string): TabsRecord {
  const saved = readSessionWorkspaceState(conversationId);
  const tabs = [...new Set(saved.openBrowsers ?? [])];
  const selected = saved.selectedBrowserId ?? null;
  return {
    version: 0,
    tabs,
    selected: selected !== null && tabs.includes(selected) ? selected : null,
    agentTab: null,
    navs: {},
  };
}

/** Whether the tabs live in main: the switch is on, in a desktop app that has them. */
function active(): boolean {
  return browserAgentToolsActive() && typeof bridge()?.browserTabs === "function";
}

/**
 * The session's tabs as this window last knew them (its own unanswered
 * changes included), or null while the tools are off, when the local
 * store keeps them itself.
 */
export function mainBrowserTabs(conversationId: string): TabsRecord | null {
  if (!active()) return null;
  subscribe();
  const known = records.get(conversationId) ?? fromWorkspace(conversationId);
  const mine = pending.get(conversationId);
  return mine ? { ...mine, version: known.version } : known;
}

/** The tab agents act on, given the session's open tabs: its record's
 *  agent tab while that's open, else `fallback` (the reserved tab). */
export function agentTabAmong(
  conversationId: string,
  open: { tabs: string[] },
  fallback: string,
): string {
  const chosen = mainBrowserTabs(conversationId)?.agentTab;
  return chosen && open.tabs.includes(chosen) ? chosen : fallback;
}

function agentTabOf(record: TabsChange): string {
  return record.agentTab !== null && record.tabs.includes(record.agentTab)
    ? record.agentTab
    : RESERVED_TAB_ID;
}

/**
 * `next` as main should store it. A change that closed the tab agents act on
 * (the reserved one included) and didn't pick another moves them to the tab
 * shown instead, as a browser does, so they neither act on a closed page nor
 * follow later user selections. Closed tabs' navigation counts go.
 */
function settle(before: TabsChange, next: TabsChange): TabsChange {
  let { agentTab } = next;
  const acting = agentTabOf(before);
  if (agentTab === before.agentTab && before.tabs.includes(acting) && !next.tabs.includes(acting)) {
    agentTab = next.selected === RESERVED_TAB_ID ? null : next.selected;
  }
  const navs = Object.fromEntries(
    Object.entries(next.navs).filter(([tabId]) => next.tabs.includes(tabId)),
  );
  return { tabs: next.tabs, selected: next.selected, agentTab, navs };
}

function sameTabs(a: TabsChange, b: TabsChange): boolean {
  return (
    JSON.stringify([a.tabs, a.selected, a.agentTab, a.navs]) ===
    JSON.stringify([b.tabs, b.selected, b.agentTab, b.navs])
  );
}

async function ask(args: Record<string, unknown>): Promise<UpdateAnswer> {
  const call = bridge()?.browserTabs;
  if (!call) throw new Error("this desktop app has no browser tab records");
  return call(args);
}

/** The session's record from main, seeding main from the saved workspace
 *  the first time, so main knows its pages are the session's only ones. */
export async function loadTabs(conversationId: string): Promise<TabsRecord> {
  subscribe();
  const answer = await ask({ op: "get", conversationId });
  if (answer.record) {
    keep(conversationId, answer.record);
    return records.get(conversationId)!;
  }
  return (await changeTabs(conversationId, (current) => current)).record;
}

// Attempts at a change that keeps losing to other windows' changes.
const MAX_ATTEMPTS = 20;

/**
 * Change the session's tabs atomically: `change` gets main's current record
 * and returns the next one, or null to leave it as it is (`applied: false`).
 * It runs again if another window changed the record first.
 *
 * With `deadlineAt` (an agent action's), the change is never made past it:
 * main refuses a late one, and this throws BrowserTabsExpired.
 */
export async function changeTabs(
  conversationId: string,
  change: (current: TabsRecord) => TabsChange | null,
  deadlineAt?: number,
): Promise<{ record: TabsRecord; applied: boolean }> {
  subscribe();
  let current = records.get(conversationId) ?? fromWorkspace(conversationId);
  for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt += 1) {
    if (deadlineAt !== undefined && Date.now() >= deadlineAt) throw new BrowserTabsExpired();
    const wanted = change(current);
    if (wanted === null) return { record: current, applied: false };
    const next = settle(current, wanted);
    // A change that matches this window's copy needs no write only if main
    // still holds that copy: another window's change, or this window's own
    // earlier write (A, then B, then A again), may not have reached it yet.
    if (current.version > 0 && sameTabs(current, next)) {
      // oxlint-disable-next-line no-await-in-loop -- Each attempt starts from the last one's answer.
      const held = (await ask({ op: "get", conversationId })).record;
      if (held?.version === current.version) return { record: current, applied: true };
      if (held) keep(conversationId, held);
      current = held ?? fromWorkspace(conversationId);
      continue;
    }
    // oxlint-disable-next-line no-await-in-loop -- Each attempt starts from the last one's answer.
    const answer = await ask({
      op: "update",
      conversationId,
      expectedVersion: current.version,
      next,
      ...(deadlineAt === undefined ? {} : { deadlineAt }),
    });
    if (answer.ok && answer.record) {
      // A newer record (another window's later change) may already be held and saved.
      if (keep(conversationId, answer.record)) persist(conversationId, answer.record);
      notify(conversationId);
      return { record: records.get(conversationId)!, applied: true };
    }
    if (answer.expired) throw new BrowserTabsExpired();
    if (!answer.conflict) throw new Error(answer.error ?? "couldn't save the browser tabs");
    current = answer.record ?? fromWorkspace(conversationId);
    if (answer.record) keep(conversationId, answer.record);
  }
  throw new Error("other windows kept changing the browser tabs; try again");
}

/**
 * The local store's synchronous change: shown at once (the returned
 * state, and `mainBrowserTabs` meanwhile), and saved to main in the
 * background, re-applied there if another window changed the tabs first.
 */
export function changeTabsNow<S extends { tabs: string[]; selected: string | null }>(
  conversationId: string,
  mutate: (current: TabsRecord) => S,
): S {
  const { shown, saved } = changeTabsNowSaved(conversationId, mutate);
  saved.catch((error: unknown) => console.warn("[browserTabs] couldn't save the tabs:", error));
  return shown;
}

/** `changeTabsNow`, with the background save as `saved`: it rejects when the save fails. */
export function changeTabsNowSaved<S extends { tabs: string[]; selected: string | null }>(
  conversationId: string,
  mutate: (current: TabsRecord) => S,
): { shown: S; saved: Promise<void> } {
  const base = mainBrowserTabs(conversationId) ?? fromWorkspace(conversationId);
  const shown = mutate(base);
  const mine = settle(base, { ...base, ...shown });
  pending.set(conversationId, mine);
  const saved = changeTabs(conversationId, (current) => ({ ...current, ...mutate(current) }))
    .then(() => undefined)
    .finally(() => {
      if (pending.get(conversationId) === mine) pending.delete(conversationId);
      notify(conversationId);
    });
  return { shown, saved };
}

/** `state` with one more agent or link navigation of `tabId`. */
export function countNavigation<S extends { tabs: string[]; selected: string | null }>(
  state: S & Partial<Pick<TabsRecord, "navs">>,
  tabId: string,
): S & Pick<TabsRecord, "navs"> {
  const navs = state.navs ?? {};
  return { ...state, navs: { ...navs, [tabId]: (navs[tabId] ?? 0) + 1 } };
}

/** Subscribe to changes of any session's tabs (this window's and others');
 *  returns an unsubscribe. */
export function onBrowserTabsChanged(listener: (conversationId: string) => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Drop main's records for this server (the switch went off): the local
 *  store takes the tabs back from the saved workspace. */
export async function resetMainBrowserTabs(): Promise<void> {
  handBackAll();
  records.clear();
  pending.clear();
  if (typeof bridge()?.browserTabs === "function") await ask({ op: "reset" });
}
