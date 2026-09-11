// Persisted, app-global preference for which host the new-session landing
// composer starts on.
//
// Mirrors agentPreferences: the landing screen keeps its live React state as
// the source of truth; these helpers only seed that state on mount and
// snapshot it when the user explicitly picks a host (or the managed sandbox),
// so the next visit starts from the last choice instead of the auto-picked
// default — the sandbox in managed deployments, the first online host in OSS.
// The consumer still validates a stored host id against the live host list. An
// unavailable stored host is never silently replaced with the automatic
// default: the picker waits for it to reappear, or for the user to explicitly
// choose another host.
//
// Also owns the sandbox-choice codec (sandboxHostChoice /
// sandboxHostChoiceProvider), which widens the stored sentinel to name one
// provider. It lives here because it extends that sentinel's grammar, and
// because a Radix `Select` carries only a string value — so a picker rendered
// as one list of targets must encode the provider into the value. The
// NewChatDialog dropdown needs no encoding: it holds the provider in its own
// React state.

const STORAGE_KEY = "omnigent:last-host-choice";
const SANDBOX_PROVIDER_KEY = "omnigent:last-sandbox-provider";

// Stored in place of a host id when the user picked the managed-sandbox option,
// which has no host id of its own (the server provisions the host at create
// time). A reserved sentinel so it can never collide with a real host id.
export const SANDBOX_HOST_CHOICE = "__sandbox__";

// Separator for the per-provider form of the sentinel, `__sandbox__:<provider>`
// (see sandboxHostChoice). Kept next to the sentinel so both halves of the
// grammar live in one place.
const SANDBOX_PROVIDER_SEPARATOR = ":";

/**
 * Widen {@link SANDBOX_HOST_CHOICE} to name one sandbox provider.
 *
 * A picker rendered as a single list of targets needs a distinct value per
 * provider row, which the bare sentinel can't give. A real host id can never
 * collide with the result, since the sentinel prefix is reserved.
 *
 * @param provider Provider id, e.g. "modal"; `null` when the server names none.
 * @returns The per-provider choice, e.g. `"__sandbox__:modal"`.
 */
export function sandboxHostChoice(provider: string | null): string {
  return `${SANDBOX_HOST_CHOICE}${SANDBOX_PROVIDER_SEPARATOR}${provider ?? ""}`;
}

/**
 * Read the provider back out of a {@link sandboxHostChoice} value.
 *
 * @param choice A picker value — either a per-provider sandbox choice or a
 *   real host id.
 * @returns The provider id; `null` for a sandbox choice naming none; and
 *   `undefined` when `choice` is a host id rather than a sandbox choice.
 */
export function sandboxHostChoiceProvider(choice: string): string | null | undefined {
  const prefix = `${SANDBOX_HOST_CHOICE}${SANDBOX_PROVIDER_SEPARATOR}`;
  if (!choice.startsWith(prefix)) return undefined;
  return choice.slice(prefix.length) || null;
}

/**
 * Read the user's last explicit host choice on the landing composer: a host
 * id, the {@link SANDBOX_HOST_CHOICE} sentinel, or `null` when nothing is
 * stored, on a server render (no `window`), or when storage is inaccessible —
 * never throws.
 */
export function readLastHostChoice(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

/**
 * Persist `choice` (a host id or {@link SANDBOX_HOST_CHOICE}) as the user's
 * last explicit host pick. Swallows quota/access errors so a failed write
 * can't break session creation.
 */
export function writeLastHostChoice(choice: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, choice);
  } catch {
    // localStorage quota or access errors shouldn't break the composer.
  }
}

/**
 * Read the user's last explicit sandbox-provider pick (e.g. `"modal"`), or
 * `null` when nothing is stored, on a server render, or when storage is
 * inaccessible — never throws. Paired with {@link SANDBOX_HOST_CHOICE}: when
 * the last host choice was the sandbox, this names which provider it launched
 * on so the composer reopens on it.
 */
export function readLastSandboxProvider(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(SANDBOX_PROVIDER_KEY);
  } catch {
    return null;
  }
}

/**
 * Persist the user's last sandbox-provider pick, or clear it when `provider`
 * is `null` (a provider-less server default). Swallows quota/access errors so
 * a failed write can't break session creation.
 */
export function writeLastSandboxProvider(provider: string | null): void {
  if (typeof window === "undefined") return;
  try {
    if (provider === null) {
      window.localStorage.removeItem(SANDBOX_PROVIDER_KEY);
    } else {
      window.localStorage.setItem(SANDBOX_PROVIDER_KEY, provider);
    }
  } catch {
    // localStorage quota or access errors shouldn't break the composer.
  }
}
