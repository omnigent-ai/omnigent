// Per-session UI state for the right "Workspace" rail, keyed by conversationId
// so each session restores its own layout: whether the rail is open, its width,
// the selected rail tab, the set of open file tabs, and the set of open shell
// tabs (plus which one is active). A brand-new session (no stored `open`)
// follows the Appearance Workspace panel default; width and tabs start empty.
// Shell tabs restore to whichever terminals still exist — the AppShell prune
// effect drops keys whose PTY has gone away once the terminal list loads.

import type { RightRailTab } from "@/shell/railTabs";
import { parseTerminalSplitState, type TerminalSplitState } from "./terminalSplitLayout";
import type { DefaultWorkspaceTab } from "./workspaceTabPreferences";

const WORKSPACE_PANEL_TABS: readonly DefaultWorkspaceTab[] = [
  "files",
  "changes",
  "github",
  "tasks",
  "subagents",
];
const RAIL_TABS: readonly RightRailTab[] = [
  ...WORKSPACE_PANEL_TABS,
  "browser",
  "sidechat",
  "content-search",
  "gadget",
];

export interface SessionWorkspaceState {
  /** Whether the rail was left open in this session. */
  open?: boolean;
  /** User-chosen rail width (px) for this session. */
  widthPx?: number;
  /** Selected Workspace page, or null when no pinned or opened page is available. */
  rightRailTab?: RightRailTab | null;
  /** Unpinned built-in pages explicitly opened from the Workspace menu. */
  openPanels?: DefaultWorkspaceTab[];
  /** Ordered list of open file tabs. */
  openFiles?: string[];
  /** The active file tab (null = a scope view is active). */
  selectedFilePath?: string | null;
  /** Ordered list of open shell tabs (``terminalTabKey`` values). */
  openTerminals?: string[];
  /** The active shell tab (null = a file/scope view is active). */
  selectedTerminalKey?: string | null;
  openBrowsers?: string[];
  selectedBrowserId?: string | null;
  /** Last page of each open browser tab, keyed by tab ID, for restoring its address and label. */
  browserPages?: Record<string, SavedBrowserPage>;
  /** Ordered list of open side-chat tabs (child conversation ids). */
  openSideChats?: string[];
  /** Side chats closed on this device; discovery must not reopen them. */
  closedSideChats?: string[];
  /** Open sub-agent tabs (`subagentTabId` values) in open order, shown after the side chats. */
  openSubagentTabs?: string[];
  /** The active side-chat tab (null = a file/scope/other view is active). */
  selectedSideChatId?: string | null;
  /** The active gadget tab (`gadget:<id>`), restored once the session's gadgets load. */
  selectedGadgetKey?: string | null;
  /** Split panes of the main terminal view; absent while unsplit. */
  terminalSplit?: TerminalSplitState;
}

/** A browser tab's last http(s) page. */
export interface SavedBrowserPage {
  url: string;
  title?: string;
  /** The signed-in user who visited it; only they may see or reopen it. */
  owner: string;
}

export const SESSION_WORKSPACE_STATE_KEY = "omnigent:session-workspace-state";
// Cap stored sessions so the store can't grow without bound. The
// least-recently-touched entries (front of the array) are pruned first once the
// cap is exceeded.
const MAX_SESSIONS = 100;
// Cap the open-file tabs persisted per session so one session that churns
// through many files can't bloat the store. Tabs are appended in open order, so
// the most-recent (tail) entries are kept.
export const MAX_OPEN_TABS = 20;
const MAX_BROWSER_URL_LENGTH = 8192;
const MAX_BROWSER_TITLE_LENGTH = 512;
// Recognized credential names, including authentication compounds. Generic suffixes
// such as country_code and sort_key are ordinary page state. This remains a heuristic.
const CREDENTIAL_PARAM_PART = new RegExp(
  [
    "token|secret|passw|signature|credential|verifier|api[-_]?key|sess(ion)?[-_]?id",
    "saml(request|response|art)",
    "(access|auth(?:orization)?|oauth|login|pass|private|secure|signing)[-_.]?(code|key|pwd|sig|ticket)",
  ].join("|"),
  "i",
);
const CREDENTIAL_PARAM_NAME = /^(auth|authorization|code|jwt|key|otp|pass|pwd|sid|sig|ticket)$/i;

function isValidWidth(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

function isRailTab(value: unknown): value is RightRailTab {
  return typeof value === "string" && (RAIL_TABS as readonly string[]).includes(value);
}

/**
 * One persisted session entry. The store is an ordered array (not a keyed
 * object) so recency ordering survives serialization regardless of the id
 * format: a plain `Record` reorders integer-like keys (e.g. numeric
 * conversation ids) into ascending numeric order, which would break both the
 * "touch = move to end" refresh and the oldest-first pruning.
 */
interface StoredEntry {
  id: string;
  state: SessionWorkspaceState;
}

type Store = StoredEntry[];

/** The non-empty strings in `values`, first occurrence kept. */
function uniqueIds(values: unknown[]): string[] {
  return [
    ...new Set(
      values.filter((value): value is string => typeof value === "string" && value !== ""),
    ),
  ];
}

function isCredentialParam(name: string): boolean {
  return CREDENTIAL_PARAM_PART.test(name) || CREDENTIAL_PARAM_NAME.test(name);
}

function decodeCredentialUrlPart(value: string): string {
  // Credential names and delimiters are ASCII; unrelated malformed escapes can stay literal.
  return value.replace(/%([0-7][0-9a-f])/gi, (_match, hex: string) =>
    String.fromCharCode(Number.parseInt(hex, 16)),
  );
}

function carriesCredential({ username, password, pathname, search, hash }: URL): boolean {
  if (username !== "" || password !== "") return true;
  // Some servers rewrite session credentials into semicolon-delimited path parameters.
  const pathParams = decodeCredentialUrlPart(pathname)
    .split("/")
    .flatMap((segment) => segment.split(";").slice(1));
  // Hash routes can carry query or semicolon-delimited path parameters.
  const fragmentQueries = decodeCredentialUrlPart(hash.slice(1))
    .split(/[?;]/)
    .filter((part) => part.includes("="));
  const params = [search, ...fragmentQueries, ...pathParams].flatMap((query) => [
    ...new URLSearchParams(query.replaceAll(";", "&")).keys(),
  ]);
  return params.some(isCredentialParam);
}

/** Whether an address may be retained for saving once its owner is known. */
export function isPersistableBrowserUrl(url: unknown): url is string {
  if (typeof url !== "string" || url.length > MAX_BROWSER_URL_LENGTH) return false;
  try {
    const parsed = new URL(url);
    return (
      (parsed.protocol === "http:" || parsed.protocol === "https:") && !carriesCredential(parsed)
    );
  } catch {
    return false;
  }
}

/** A saved page, or null without an owner or for an invalid URL or recognized credential. */
export function sanitizeSavedBrowserPage(value: unknown): SavedBrowserPage | null {
  if (typeof value !== "object" || value === null) return null;
  const { url, title, owner } = value as Record<string, unknown>;
  if (typeof owner !== "string" || owner === "" || !isPersistableBrowserUrl(url)) return null;
  const page: SavedBrowserPage = { url, owner };
  if (typeof title === "string" && title.trim() !== "") {
    page.title = title.slice(0, MAX_BROWSER_TITLE_LENGTH);
  }
  return page;
}

function sanitizeSavedBrowserPages(value: unknown): Record<string, SavedBrowserPage> | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined;
  // Tab IDs come from storage, so a `__proto__` key must stay an ordinary entry.
  const pages: Record<string, SavedBrowserPage> = Object.create(null);
  for (const [tabId, raw] of Object.entries(value)) {
    const page = sanitizeSavedBrowserPage(raw);
    if (tabId !== "" && page) pages[tabId] = page;
  }
  return pages;
}

function sanitize(entry: unknown): SessionWorkspaceState {
  if (typeof entry !== "object" || entry === null) return {};
  const record = entry as Record<string, unknown>;
  const state: SessionWorkspaceState = {};
  if (typeof record.open === "boolean") state.open = record.open;
  if (isValidWidth(record.widthPx)) state.widthPx = record.widthPx;
  if (record.rightRailTab === null || isRailTab(record.rightRailTab)) {
    state.rightRailTab = record.rightRailTab;
  }
  if (Array.isArray(record.openPanels)) {
    state.openPanels = [
      ...new Set(
        record.openPanels.filter((value): value is DefaultWorkspaceTab =>
          WORKSPACE_PANEL_TABS.some((tab) => tab === value),
        ),
      ),
    ];
  }
  if (Array.isArray(record.openFiles) && record.openFiles.every((p) => typeof p === "string")) {
    state.openFiles = record.openFiles as string[];
  }
  if (record.selectedFilePath === null || typeof record.selectedFilePath === "string") {
    state.selectedFilePath = record.selectedFilePath;
  }
  if (
    Array.isArray(record.openTerminals) &&
    record.openTerminals.every((k) => typeof k === "string")
  ) {
    state.openTerminals = record.openTerminals as string[];
  }
  if (record.selectedTerminalKey === null || typeof record.selectedTerminalKey === "string") {
    state.selectedTerminalKey = record.selectedTerminalKey;
  }
  if (Array.isArray(record.openBrowsers)) state.openBrowsers = uniqueIds(record.openBrowsers);
  if (record.selectedBrowserId === null || typeof record.selectedBrowserId === "string") {
    state.selectedBrowserId = record.selectedBrowserId;
  }
  const browserPages = sanitizeSavedBrowserPages(record.browserPages);
  if (browserPages) state.browserPages = browserPages;
  if (Array.isArray(record.openSideChats)) state.openSideChats = uniqueIds(record.openSideChats);
  if (Array.isArray(record.closedSideChats))
    state.closedSideChats = uniqueIds(record.closedSideChats);
  if (Array.isArray(record.openSubagentTabs))
    state.openSubagentTabs = uniqueIds(record.openSubagentTabs);
  if (record.selectedSideChatId === null || typeof record.selectedSideChatId === "string") {
    state.selectedSideChatId = record.selectedSideChatId;
  }
  if (record.selectedGadgetKey === null || typeof record.selectedGadgetKey === "string") {
    state.selectedGadgetKey = record.selectedGadgetKey;
  }
  const terminalSplit = parseTerminalSplitState(record.terminalSplit);
  if (terminalSplit) state.terminalSplit = terminalSplit;
  return state;
}

function readStore(): Store {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(SESSION_WORKSPACE_STATE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const store: Store = [];
    for (const item of parsed) {
      if (typeof item !== "object" || item === null) continue;
      const record = item as Record<string, unknown>;
      if (typeof record.id !== "string") continue;
      store.push({ id: record.id, state: sanitize(record.state) });
    }
    return store;
  } catch {
    return [];
  }
}

/** Persist the store; false when storage refused the write. */
function writeStore(store: Store): boolean {
  if (typeof window === "undefined") return false;
  try {
    window.localStorage.setItem(SESSION_WORKSPACE_STATE_KEY, JSON.stringify(store));
    return true;
  } catch (error) {
    if (
      !(error instanceof DOMException) ||
      error.name !== "QuotaExceededError" ||
      !store.some(({ state }) => state.browserPages)
    ) {
      return false;
    }
  }
  // Page metadata is expendable when it prevents workspace settings from being saved.
  for (const { state } of store) delete state.browserPages;
  try {
    window.localStorage.setItem(SESSION_WORKSPACE_STATE_KEY, JSON.stringify(store));
    return true;
  } catch {
    return false;
  }
}

/** Read one session's persisted workspace state (absent/invalid fields omitted). */
export function readSessionWorkspaceState(conversationId: string): SessionWorkspaceState {
  const entry = readStore().find((e) => e.id === conversationId);
  return entry ? entry.state : {};
}

/** Like {@link writeSessionWorkspaceState}, but returns false when storage refused the write. */
export function saveSessionWorkspaceState(
  conversationId: string,
  patch: SessionWorkspaceState,
): boolean {
  const store = readStore();
  const existingIdx = store.findIndex((e) => e.id === conversationId);
  const prev = existingIdx >= 0 ? store[existingIdx].state : {};
  const next = { ...prev, ...patch };
  // Keep only the most-recent open-file tabs (tabs are appended in open order).
  if (next.openFiles && next.openFiles.length > MAX_OPEN_TABS) {
    next.openFiles = next.openFiles.slice(-MAX_OPEN_TABS);
  }
  // Same bound for shell tabs (also appended in open order).
  if (next.openTerminals && next.openTerminals.length > MAX_OPEN_TABS) {
    next.openTerminals = next.openTerminals.slice(-MAX_OPEN_TABS);
  }
  // Same bound for browser tabs. Native view creation enforces its own lower
  // runtime cap; this keeps the persisted rail from growing without bound.
  if (next.openBrowsers && next.openBrowsers.length > MAX_OPEN_TABS) {
    next.openBrowsers = next.openBrowsers.slice(-MAX_OPEN_TABS);
  }
  // A closed tab forgets its page. Only a tab-list write prunes: with the agent browser
  // tools on, a newly opened tab's page can be saved before main's tab list reaches here.
  if (patch.openBrowsers && next.browserPages) {
    const open = new Set(next.openBrowsers ?? []);
    next.browserPages = Object.fromEntries(
      Object.entries(next.browserPages).filter(([tabId]) => open.has(tabId)),
    );
  }
  // Same bound for side-chat tabs (also appended in open order).
  if (next.openSideChats && next.openSideChats.length > MAX_OPEN_TABS) {
    next.openSideChats = next.openSideChats.slice(-MAX_OPEN_TABS);
  }
  if (next.openSubagentTabs && next.openSubagentTabs.length > MAX_OPEN_TABS) {
    next.openSubagentTabs = next.openSubagentTabs.slice(-MAX_OPEN_TABS);
  }
  // Drop any existing entry and re-append so the most-recently-touched session
  // moves to the end; pruning then evicts from the front (oldest-touched).
  if (existingIdx >= 0) store.splice(existingIdx, 1);
  store.push({ id: conversationId, state: next });
  if (store.length > MAX_SESSIONS) {
    store.splice(0, store.length - MAX_SESSIONS);
  }
  return writeStore(store);
}

/** Merge a partial update into one session's persisted workspace state. */
export function writeSessionWorkspaceState(
  conversationId: string,
  patch: SessionWorkspaceState,
): void {
  saveSessionWorkspaceState(conversationId, patch);
}

/** Let existing chats follow a changed default while keeping their open tabs and layout. */
export function resetSessionWorkspaceTabSelections(): void {
  const store = readStore();
  for (const { state } of store) {
    delete state.rightRailTab;
    delete state.selectedFilePath;
    delete state.selectedTerminalKey;
    delete state.selectedBrowserId;
    delete state.selectedSideChatId;
    delete state.selectedGadgetKey;
  }
  writeStore(store);
}

/** Forget newly unpinned pages without disturbing other tabs or their selection. */
export function unpinSessionWorkspaceTabs(tabs: readonly DefaultWorkspaceTab[]): void {
  if (tabs.length === 0) return;
  const store = readStore();
  for (const { state } of store) {
    if (tabs.some((tab) => tab === state.rightRailTab)) delete state.rightRailTab;
    if (state.openPanels) state.openPanels = state.openPanels.filter((tab) => !tabs.includes(tab));
  }
  writeStore(store);
}
