import { useSyncExternalStore } from "react";
import { DEFAULT_KEYBINDINGS } from "./defaultKeybindings";
import {
  isUserKeybindingRuleUsable,
  resolveEffectiveKeymap,
  type EffectiveKeymap,
  type KeybindingConflict,
} from "./effectiveKeymap";
import {
  KEYBINDINGS_STORAGE_KEY,
  MAX_USER_KEYBINDINGS,
  normalizeUserKeybindingRule,
  parseUserKeybindingPreferences,
  writeUserKeybindings,
  type KnownUserKeybindingRule,
  type UserKeybindingRule,
} from "./keybindingPreferences";
import type { ActionId, KeybindingMode, KeybindingRule } from "./types";

export interface KeybindingSnapshot {
  /** Join default/effective/user rows by stable rule id, never object identity. */
  defaultRules: readonly KeybindingRule[];
  /** Identity rows can arrive from storage; derive Modified from effectiveRules.origin. */
  userRules: readonly UserKeybindingRule[];
  effectiveRules: readonly KeybindingRule[];
  conflicts: readonly KeybindingConflict[];
}

export type KeybindingMutationResult =
  | { ok: true; changed: boolean }
  | {
      ok: false;
      reason: "invalidRule" | "unusableRule" | "limitReached" | "storageUnavailable";
    };

const listeners = new Set<() => void>();
let initialized = false;
let cachedRaw: string | null = null;
let cachedSnapshot: KeybindingSnapshot | undefined;
let defaultKeymap: EffectiveKeymap | undefined;
let serverSnapshot: KeybindingSnapshot | undefined;
let listeningToStorage = false;

function getDefaultKeymap(): EffectiveKeymap {
  defaultKeymap ??= resolveEffectiveKeymap(DEFAULT_KEYBINDINGS, []);
  return defaultKeymap;
}

function snapshotFor(raw: string | null): KeybindingSnapshot {
  const userRules = parseUserKeybindingPreferences(raw);
  const effective = resolveEffectiveKeymap(DEFAULT_KEYBINDINGS, userRules);
  return Object.freeze({
    defaultRules: getDefaultKeymap().rules,
    userRules,
    effectiveRules: effective.rules,
    conflicts: effective.conflicts,
  });
}

function getServerSnapshot(): KeybindingSnapshot {
  serverSnapshot ??= snapshotFor(null);
  return serverSnapshot;
}

function readRaw(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(KEYBINDINGS_STORAGE_KEY);
  } catch {
    return null;
  }
}

function applyRaw(raw: string | null): boolean {
  if (initialized && cachedSnapshot && raw === cachedRaw) return false;
  cachedRaw = raw;
  cachedSnapshot = snapshotFor(raw);
  initialized = true;
  return true;
}

export function getKeybindingSnapshot(): KeybindingSnapshot {
  if (typeof window === "undefined") return getServerSnapshot();
  // Before the first subscription there is no storage listener, so imperative reads refresh.
  if (!initialized || !cachedSnapshot || !listeningToStorage) applyRaw(readRaw());
  return cachedSnapshot!;
}

function notify(raw: string | null = readRaw()): void {
  if (!applyRaw(raw)) return;
  listeners.forEach((listener) => listener());
}

function onStorage(event: StorageEvent): void {
  if (event.key !== null && event.key !== KEYBINDINGS_STORAGE_KEY) return;
  notify(event.key === null ? null : event.newValue);
}

function startStorageListener(): void {
  if (listeningToStorage || typeof window === "undefined") return;
  window.addEventListener("storage", onStorage);
  listeningToStorage = true;
}

function stopStorageListener(): void {
  if (!listeningToStorage || typeof window === "undefined") return;
  window.removeEventListener("storage", onStorage);
  listeningToStorage = false;
}

export function subscribeKeybindings(listener: () => void): () => void {
  listeners.add(listener);
  if (listeners.size === 1) applyRaw(readRaw());
  startStorageListener();
  return () => {
    listeners.delete(listener);
  };
}

export function useKeybindingSnapshot(): KeybindingSnapshot {
  return useSyncExternalStore(subscribeKeybindings, getKeybindingSnapshot, getServerSnapshot);
}

function applyUserKeybindingRule(rule: UserKeybindingRule): KeybindingMutationResult {
  const normalized = normalizeUserKeybindingRule(rule);
  if (!normalized) return { ok: false, reason: "invalidRule" };
  if (!isUserKeybindingRuleUsable(DEFAULT_KEYBINDINGS, normalized)) {
    return { ok: false, reason: "unusableRule" };
  }
  const current = getKeybindingSnapshot();
  const existing = current.userRules.find((candidate) => candidate.id === normalized.id);
  if (existing && JSON.stringify(existing) === JSON.stringify(normalized)) {
    return { ok: true, changed: false };
  }
  const target = current.defaultRules.find((candidate) => candidate.id === normalized.id);
  if (
    target &&
    normalized.sequence !== null &&
    resolveEffectiveKeymap([target], [normalized]).rules[0]?.origin === "default"
  ) {
    return resetUserKeybindingRule(normalized.id);
  }
  const next = [
    ...current.userRules.filter((candidate) => candidate.id !== normalized.id),
    normalized,
  ];
  if (next.length > MAX_USER_KEYBINDINGS) return { ok: false, reason: "limitReached" };
  if (!writeUserKeybindings(next)) return { ok: false, reason: "storageUnavailable" };
  notify();
  return { ok: true, changed: true };
}

/** Add an alternate or replace an existing override with the same stable id. */
export function setUserKeybindingRule(rule: KnownUserKeybindingRule): KeybindingMutationResult {
  return applyUserKeybindingRule(rule);
}

/** Runtime-validated counterpart for catalog-driven editors with a dynamic action id. */
export function setUserKeybindingCandidate(rule: UserKeybindingRule): KeybindingMutationResult {
  return applyUserKeybindingRule(rule);
}

/**
 * Replace all structurally valid overrides for import flows. Future actions and
 * semantically unusable known rows remain dormant so round-trips are lossless.
 */
export function replaceAllUserKeybindings(
  rules: readonly UserKeybindingRule[],
): KeybindingMutationResult {
  if (rules.length > MAX_USER_KEYBINDINGS) return { ok: false, reason: "limitReached" };
  const normalized: UserKeybindingRule[] = [];
  for (const candidate of rules) {
    const rule = normalizeUserKeybindingRule(candidate);
    if (!rule) return { ok: false, reason: "invalidRule" };
    normalized.push(rule);
  }
  const current = getKeybindingSnapshot().userRules;
  if (JSON.stringify(normalized) === JSON.stringify(current)) return { ok: true, changed: false };
  if (!writeUserKeybindings(normalized)) return { ok: false, reason: "storageUnavailable" };
  notify();
  return { ok: true, changed: true };
}

/** Remove an override so the current product default is visible again. */
export function resetUserKeybindingRule(id: string): KeybindingMutationResult {
  const current = getKeybindingSnapshot().userRules;
  const next = current.filter((candidate) => candidate.id !== id);
  if (next.length === current.length) return { ok: true, changed: false };
  if (!writeUserKeybindings(next)) return { ok: false, reason: "storageUnavailable" };
  notify();
  return { ok: true, changed: true };
}

export function resetAllUserKeybindings(): KeybindingMutationResult {
  const snapshot = getKeybindingSnapshot();
  if (snapshot.userRules.length === 0 && readRaw() === null) return { ok: true, changed: false };
  if (!writeUserKeybindings([])) return { ok: false, reason: "storageUnavailable" };
  notify(null);
  return { ok: true, changed: true };
}

/** Unbind a catalog default identified by id; action and mode validate the caller's row. */
export function unbindDefaultKeybinding<A extends ActionId>(rule: {
  id: string;
  action: A;
  mode: KeybindingMode;
}): KeybindingMutationResult {
  return applyUserKeybindingRule({
    id: rule.id,
    action: rule.action,
    mode: rule.mode,
    sequence: null,
  });
}

/** Clear module caches between tests without mutating persisted data or live subscriptions. */
export function resetKeybindingStoreForTesting(): void {
  initialized = false;
  cachedRaw = null;
  cachedSnapshot = undefined;
  defaultKeymap = undefined;
  serverSnapshot = undefined;
  if (listeners.size === 0) {
    stopStorageListener();
  } else {
    notify();
  }
}
