// Tests only: a desktop app's main-process tab records, as
// the `browserTabs` bridge answers (electron/src/browserTabs.js), with
// a way to change them as another window would.
import type { TabsRecord } from "./tabStore";

type Next = Omit<TabsRecord, "version">;
interface Pushed {
  conversationId?: string;
  record?: TabsRecord;
  reset?: true;
}

export interface FakeMainTabs {
  browserTabs: (args: Record<string, unknown>) => Promise<Record<string, unknown>>;
  onBrowserTabsChanged: (callback: (payload: Pushed) => void) => () => void;
  /** The record main holds, or undefined. */
  record: (conversationId: string) => TabsRecord | undefined;
  /** Another window commits `change` to the record (with its push). */
  otherWindow: (conversationId: string, change: (current: Next) => Next) => void;
  /** Every update this window sent, oldest first. */
  updates: Record<string, unknown>[];
  /** Hold main's pushes to this window until the returned function runs. */
  holdPushes: () => () => void;
}

export function fakeMainTabs(): FakeMainTabs {
  const records = new Map<string, TabsRecord>();
  const subscribers = new Set<(payload: Pushed) => void>();
  const updates: Record<string, unknown>[] = [];
  let held: Pushed[] | null = null;
  const push = (payload: Pushed) => {
    if (held) held.push(payload);
    else for (const subscriber of subscribers) subscriber(payload);
  };
  const commit = (conversationId: string, next: Next) => {
    const record = { ...next, version: (records.get(conversationId)?.version ?? 0) + 1 };
    records.set(conversationId, record);
    push({ conversationId, record });
    return record;
  };
  const empty: Next = { tabs: [], selected: null, agentTab: null, navs: {} };
  return {
    updates,
    holdPushes: () => {
      held = [];
      return () => {
        const queued = held ?? [];
        held = null;
        queued.forEach(push);
      };
    },
    record: (conversationId) => records.get(conversationId),
    otherWindow: (conversationId, change) => {
      const { version: _version, ...current } = records.get(conversationId) ?? {
        ...empty,
        version: 0,
      };
      commit(conversationId, change(current));
    },
    onBrowserTabsChanged: (callback) => {
      subscribers.add(callback);
      return () => subscribers.delete(callback);
    },
    browserTabs: async (args) => {
      const conversationId = String(args.conversationId);
      const current = records.get(conversationId) ?? null;
      if (args.op === "get") return { ok: true, record: current };
      if (args.op === "reset") {
        records.clear();
        for (const subscriber of subscribers) subscriber({ reset: true });
        return { ok: true };
      }
      updates.push(args);
      if (typeof args.deadlineAt === "number" && Date.now() > args.deadlineAt) {
        return { ok: false, expired: true };
      }
      if ((current?.version ?? 0) !== args.expectedVersion) {
        return { ok: false, conflict: true, record: current };
      }
      return { ok: true, record: commit(conversationId, args.next as Next) };
    },
  };
}
