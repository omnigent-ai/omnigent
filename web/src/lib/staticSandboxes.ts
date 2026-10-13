// Static sandboxes: the user's own long-lived sandboxes, saved as New
// Session hosts. The list lives with each provider; see staticSandboxesApi.ts
// for the server calls.

export type StaticSandboxStatus = "running" | "paused" | "gone";

export interface AvailableSandbox {
  // The sandbox provider, e.g. "remote".
  provider: string;
  // The sandbox's id at the provider, e.g. "foo".
  id: string;
  status: StaticSandboxStatus;
  repo: string;
  // RFC 3339 timestamp of the VM's last activity, when the provider knows it.
  lastActivityAt: string | null;
}

// Wire shape naming one static sandbox, e.g. the one a host runs on.
export interface StaticSandboxRef {
  provider: string;
  sandbox_id: string;
}

// A static sandbox: one VM, shown in the host menu under the user's label.
export interface StaticSandbox extends AvailableSandbox {
  label: string;
}

/**
 * The provider to name in static sandbox copy: the server's only static
 * sandbox provider, or null when it offers several (or none).
 */
export function soleStaticSandboxProvider(providers: readonly string[] | undefined): string | null {
  return providers !== undefined && providers.length === 1 ? providers[0] : null;
}

// Select value for a static sandbox in host pickers that also hold host
// ids; host ids never start with this prefix.
const STATIC_SANDBOX_CHOICE_PREFIX = "__static_sandbox__:";

// Provider names never contain "/", so the first one splits provider from id.
export function staticSandboxHostChoice(ref: StaticSandboxRef): string {
  return `${STATIC_SANDBOX_CHOICE_PREFIX}${ref.provider}/${ref.sandbox_id}`;
}

/** The static sandbox a {@link staticSandboxHostChoice} value names, else null. */
export function staticSandboxRefFromChoice(choice: string): StaticSandboxRef | null {
  if (!choice.startsWith(STATIC_SANDBOX_CHOICE_PREFIX)) return null;
  const rest = choice.slice(STATIC_SANDBOX_CHOICE_PREFIX.length);
  const slash = rest.indexOf("/");
  if (slash <= 0 || slash === rest.length - 1) return null;
  return { provider: rest.slice(0, slash), sandbox_id: rest.slice(slash + 1) };
}

/** Badge text for a VM's status: a paused VM is wakeable, not unavailable. */
export function staticSandboxStatusLabel(status: StaticSandboxStatus): string {
  return status === "running" ? "Running" : status === "gone" ? "Gone" : "Paused";
}
export const STATIC_SANDBOX_LABEL_MAX_LENGTH = 64;

export function normalizeStaticSandboxLabel(label: string, id: string): string {
  const trimmed = label.trim().slice(0, STATIC_SANDBOX_LABEL_MAX_LENGTH);
  return trimmed === "" ? id : trimmed;
}

/** "3 h ago"-style text for a VM's last activity, or null when unknown. */
export function staticSandboxLastActiveText(
  at: string | null,
  now: number = Date.now(),
): string | null {
  if (at === null) return null;
  const then = Date.parse(at);
  if (Number.isNaN(then)) return null;
  const minutes = Math.max(0, Math.round((now - then) / 60_000));
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}
