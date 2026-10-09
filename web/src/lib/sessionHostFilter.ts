// Which machine the sidebar's Sessions list is narrowed to ("All machines",
// "Local machine", one connected host, or every sandbox of one provider).
//
// Filtering is client-side: every session row already carries its `host_id`,
// and host names come from the `/v1/hosts` list the sidebar loads anyway. The
// pick is a device-local view preference, so it lives in localStorage like
// the display filter in `sessionFilterPreferences`.

import type { Conversation } from "@/hooks/useConversations";
import type { Host } from "@/hooks/useHosts";
import { sandboxOptionLabel } from "@/lib/capabilities";

const STORAGE_KEY = "omnigent:session-host-filter";

/**
 * `"all"`, `"local"` (sessions with no host), `host:<host_id>`, or
 * `sandbox:<provider>`. Sandboxes are grouped per provider because they are
 * created on demand — one option per sandbox would flood the menu.
 */
export type SessionHostFilter = "all" | "local" | `host:${string}` | `sandbox:${string}`;

export const ALL_HOSTS: SessionHostFilter = "all";

const LOCAL_LABEL = "Local machine";

export interface SessionHostFilterOption {
  value: SessionHostFilter;
  label: string;
  /** Live status for a known connected host; absent when unknown or grouped. */
  status?: Host["status"];
}

function isSessionHostFilter(raw: string): raw is SessionHostFilter {
  return raw === "all" || raw === "local" || /^(host|sandbox):.+$/.test(raw);
}

/**
 * Read the persisted machine filter. Falls back to {@link ALL_HOSTS} when
 * nothing usable is stored or storage is inaccessible — never throws.
 */
export function readSessionHostFilter(): SessionHostFilter {
  if (typeof window === "undefined") return ALL_HOSTS;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw !== null && isSessionHostFilter(raw) ? raw : ALL_HOSTS;
  } catch {
    return ALL_HOSTS;
  }
}

/** Persist the machine filter, swallowing quota/access errors. */
export function writeSessionHostFilter(value: SessionHostFilter): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, value);
  } catch {
    // A local view preference; losing it is harmless.
  }
}

/** The filter value a session falls under. */
export function sessionHostKey(
  conversation: Pick<Conversation, "host_id">,
  hostsById: ReadonlyMap<string, Host>,
): SessionHostFilter {
  const hostId = conversation.host_id;
  if (!hostId) return "local";
  const provider = hostsById.get(hostId)?.sandbox_provider;
  return provider ? `sandbox:${provider}` : `host:${hostId}`;
}

export function matchesSessionHostFilter(
  conversation: Pick<Conversation, "host_id">,
  filter: SessionHostFilter,
  hostsById: ReadonlyMap<string, Host>,
): boolean {
  return filter === ALL_HOSTS || sessionHostKey(conversation, hostsById) === filter;
}

function fallbackLabel(value: SessionHostFilter): string {
  if (value === "local") return LOCAL_LABEL;
  if (value.startsWith("sandbox:")) return sandboxOptionLabel(value.slice("sandbox:".length));
  // A host outside the viewer's own list (e.g. a shared session's host, or a
  // stored pick whose host was re-keyed) has no name to show; its id is unique.
  return value.slice("host:".length);
}

/**
 * The machines to offer, excluding "All machines": the viewer's hosts, plus
 * any machine only seen on loaded sessions, plus the current pick so a stale
 * selection always has a menu entry to leave it by. "Local machine" first,
 * then the rest by label.
 */
export function sessionHostFilterOptions(
  conversations: readonly Pick<Conversation, "host_id">[],
  hosts: readonly Host[],
  selected: SessionHostFilter,
): SessionHostFilterOption[] {
  const hostsById = new Map(hosts.map((host) => [host.host_id, host] as const));
  const options = new Map<SessionHostFilter, SessionHostFilterOption>();
  for (const host of hosts) {
    const value = sessionHostKey(host, hostsById);
    if (options.has(value)) continue;
    options.set(
      value,
      host.sandbox_provider
        ? { value, label: sandboxOptionLabel(host.sandbox_provider) }
        : { value, label: host.name, status: host.status },
    );
  }
  for (const conversation of conversations) {
    const value = sessionHostKey(conversation, hostsById);
    if (!options.has(value)) options.set(value, { value, label: fallbackLabel(value) });
  }
  if (selected !== ALL_HOSTS && !options.has(selected)) {
    options.set(selected, { value: selected, label: fallbackLabel(selected) });
  }
  return [...options.values()].sort((a, b) => {
    if (a.value === "local") return -1;
    if (b.value === "local") return 1;
    return a.label.localeCompare(b.label, undefined, { sensitivity: "base" });
  });
}
