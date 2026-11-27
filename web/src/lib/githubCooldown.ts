// When GitHub rate-limits this Mac's reads as one login, the page waits it out as one client:
// one cooldown per host and reader, held until the shell's deadline or, from a shell that names
// none, a fallback that doubles only when the first read after a fallback wait is limited again.

import type {
  CodeHostQueryRequest,
  GitHubFastTrackError,
  GitHubRateLimit,
} from "@/lib/nativeBridge";

/** The first wait when GitHub's limit came without a deadline. */
export const FALLBACK_COOLDOWN_MS = 60_000;
export const MAX_FALLBACK_COOLDOWN_MS = 15 * 60_000;
/** Up to this share is added to a fallback wait, so windows and logins don't retry in step. */
export const FALLBACK_JITTER = 0.2;
// A long cooldown wakes in chunks and checks its absolute deadline again each time.
const MAX_TIMER_MS = 10 * 60_000;

export const RATE_LIMITED_MESSAGE = "GitHub is limiting requests from this Mac.";

/**
 * - `waiting`: reads as this reader are held until `until`.
 * - `recovering`: the wait is over and the next read decides; nothing has answered yet.
 *
 * `limit`: which limit GitHub applied, when a newer shell named it.
 */
export type GitHubCooldownPhase =
  | { phase: "waiting"; until: number; limit?: GitHubRateLimit }
  | { phase: "recovering"; until: number; limit?: GitHubRateLimit }
  | null;

/** A read the page refused itself, without asking the shell, while its reader waits. */
export interface LocalRateLimitRefusal extends GitHubFastTrackError {
  code: "rate_limited";
  retryAt: number;
  local: true;
}

interface Episode {
  until: number;
  backoffMs: number;
  /** Whether `until` is a fallback wait, rather than a deadline the shell named. */
  fallback: boolean;
  limit?: GitHubRateLimit;
}

/** A shell's limit, kept only when it names something: an unknown kind and resource say nothing. */
function namedLimit(limit: GitHubRateLimit | null | undefined): GitHubRateLimit | undefined {
  if (!limit || (limit.kind === "unknown" && limit.resource === "unknown")) return undefined;
  return limit;
}

/** The cooldown a GitHub read as `login` on `host` waits on. */
export function githubCooldownKey(host: string, login: string): string {
  return `${host.toLowerCase()}\n${login.toLowerCase()}`;
}

interface AutomaticReaders {
  /** Who the Mac's own pick for a host reads as, once proven; null before that. */
  known: (host: string) => string | null;
  /** The same, asking the shell for it first when nothing has proven it yet. */
  discover: (host: string) => Promise<string | null>;
}

// Set by the read path, which tracks the account this Mac proved for each host.
let automaticReaders: AutomaticReaders = { known: () => null, discover: async () => null };

/** Let the read path that tracks this Mac's proven account per host name Automatic's reader. */
export function resolveAutomaticGitHubReaderWith(readers: AutomaticReaders): void {
  automaticReaders = readers;
}

/**
 * The cooldown reads as `namedLogin`, or as the Mac's own pick for `host`, wait on: one per
 * login, so a named and an Automatic read as the same account share it, and another account
 * never waits on it. Null while Automatic's account isn't known yet.
 */
export function githubReaderCooldownKey(host: string, namedLogin: string | null): string | null {
  const login = namedLogin ?? automaticReaders.known(host.toLowerCase());
  return login ? githubCooldownKey(host, login) : null;
}

/**
 * The cooldown a code-host request reads under, as far as is known now; null for one that
 * isn't a GitHub read, or an Automatic read whose account isn't proven yet.
 */
export function requestCooldownKey(request: CodeHostQueryRequest): string | null {
  if (request.provider !== "github" || UNPACED_OPERATIONS.has(request.operation)) return null;
  return githubReaderCooldownKey(request.host, request.macLogin ?? request.login ?? null);
}

/**
 * The cooldown an Automatic GitHub read whose account isn't proven yet reads under, asking the
 * shell who it reads as, so even a cold start's limit is held; null when it names nobody.
 */
export async function discoverRequestCooldownKey(
  request: CodeHostQueryRequest,
): Promise<string | null> {
  if (request.provider !== "github" || UNPACED_OPERATIONS.has(request.operation)) return null;
  const named = request.macLogin ?? request.login ?? null;
  const login = named ?? (await automaticReaders.discover(request.host.toLowerCase()));
  return login ? githubCooldownKey(request.host, login) : null;
}

// Answered from the shell's own state, never from GitHub.
const UNPACED_OPERATIONS = new Set([
  "account_list",
  "account_status",
  "organization_members",
  "recent_reviewers",
]);

/** Whether a read failed because GitHub, or the page within a cooldown, held it back. */
export function isRateLimitFailure(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    (error as { code?: unknown }).code === "rate_limited"
  );
}

/** Whether a failure is the page's own refusal within a cooldown, not GitHub's answer. */
export function isLocalRateLimitRefusal(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    (error as { local?: unknown }).local === true &&
    (error as { code?: unknown }).code === "rate_limited"
  );
}

export function createGitHubCooldowns({
  now = () => Date.now(),
  random = Math.random,
  setTimer = (callback: () => void, ms: number) => setTimeout(callback, ms),
  clearTimer = (timer: ReturnType<typeof setTimeout>) => clearTimeout(timer),
}: {
  now?: () => number;
  random?: () => number;
  setTimer?: (callback: () => void, ms: number) => ReturnType<typeof setTimeout>;
  clearTimer?: (timer: ReturnType<typeof setTimeout>) => void;
} = {}) {
  const episodes = new Map<string, Episode>();
  // Each reader's latest deadline, kept after its cooldown ends, so held reads wake from it.
  const deadlines = new Map<string, number>();
  const listeners = new Set<() => void>();
  let version = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;

  function changed() {
    for (const [key, episode] of episodes) deadlines.set(key, episode.until);
    version += 1;
    schedule();
    for (const listener of [...listeners]) listener();
  }

  // One timer for the nearest deadline still ahead, so subscribers hear when a wait ends.
  function schedule() {
    if (timer !== null) clearTimer(timer);
    timer = null;
    const time = now();
    const next = Math.min(
      ...[...episodes.values()].map((episode) => episode.until).filter((until) => until > time),
    );
    if (!Number.isFinite(next)) return;
    timer = setTimer(
      () => {
        timer = null;
        if (now() >= next) changed();
        else schedule();
      },
      Math.min(next - time, MAX_TIMER_MS),
    );
  }

  function fallbackDeadline(time: number, backoffMs: number): number {
    return time + Math.round(backoffMs * (1 + random() * FALLBACK_JITTER));
  }

  /**
   * GitHub limited a read as `key`. Within a wait, only a later deadline the shell named
   * extends it; the first read after a wait, limited again, doubles the fallback.
   */
  function rateLimited(key: string, retryAt: number | null, limit: GitHubRateLimit | null = null) {
    const named = namedLimit(limit);
    const time = now();
    // A deadline already passed says only that GitHub limited the read: the fallback applies.
    const deadline =
      retryAt !== null && Number.isFinite(retryAt) && retryAt > time ? retryAt : null;
    const episode = episodes.get(key);
    if (episode && episode.until > time) {
      // Within a wait, a named limit replaces one that said nothing, and goes with a later deadline.
      const later = deadline !== null && deadline > episode.until;
      const names = named !== undefined && (later || episode.limit === undefined);
      if (!later && !names) return;
      if (later) {
        // A later deadline sets its own limit, named or not, so an older one isn't shown with it.
        episode.until = deadline;
        episode.fallback = false;
        episode.limit = named;
      } else if (names) episode.limit = named;
    } else if (episode) {
      // Only a fallback wait that proved too short doubles the next; a deadline the shell named
      // says nothing about how long a fallback should be.
      if (deadline === null && episode.fallback) {
        episode.backoffMs = Math.min(episode.backoffMs * 2, MAX_FALLBACK_COOLDOWN_MS);
      }
      episode.until = deadline ?? fallbackDeadline(time, episode.backoffMs);
      episode.fallback = deadline === null;
      episode.limit = named;
    } else {
      episodes.set(key, {
        backoffMs: FALLBACK_COOLDOWN_MS,
        until: deadline ?? fallbackDeadline(time, FALLBACK_COOLDOWN_MS),
        fallback: deadline === null,
        ...(named ? { limit: named } : {}),
      });
    }
    changed();
  }

  /**
   * A read as `key` answered without a limit. Within a wait it started before the limit and
   * says nothing; after it, the cooldown is over, whatever else went wrong.
   */
  function answered(key: string) {
    const episode = episodes.get(key);
    if (!episode || episode.until > now()) return;
    episodes.delete(key);
    changed();
  }

  function phase(key: string): GitHubCooldownPhase {
    const episode = episodes.get(key);
    if (!episode) return null;
    const limit = episode.limit ? { limit: episode.limit } : {};
    return episode.until > now()
      ? { phase: "waiting", until: episode.until, ...limit }
      : { phase: "recovering", until: episode.until, ...limit };
  }

  /** When reads as `key` may go out again, or null when nothing holds them. */
  function heldUntil(key: string): number | null {
    const episode = episodes.get(key);
    return episode && episode.until > now() ? episode.until : null;
  }

  /** The latest deadline reads as `key` waited for, held or already passed; null if none. */
  function lastDeadline(key: string): number | null {
    return deadlines.get(key) ?? null;
  }

  /** Whether any reader on `host` is waiting, so a read whose reader isn't known yet may be. */
  function holdsHost(host: string): boolean {
    const prefix = githubCooldownKey(host, "");
    return [...episodes.keys()].some((key) => key.startsWith(prefix) && heldUntil(key) !== null);
  }

  /** The refusal a read as `key` gets now, or null when it may go out. */
  function refusal(key: string): LocalRateLimitRefusal | null {
    const until = heldUntil(key);
    if (until === null) return null;
    return { code: "rate_limited", message: RATE_LIMITED_MESSAGE, retryAt: until, local: true };
  }

  return {
    rateLimited,
    answered,
    phase,
    heldUntil,
    lastDeadline,
    holdsHost,
    refusal,
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    version: () => version,
    /** Tests only: forget every cooldown. */
    reset() {
      episodes.clear();
      deadlines.clear();
      changed();
    },
  };
}

export type GitHubCooldowns = ReturnType<typeof createGitHubCooldowns>;

/** This window's cooldowns, shared by every GitHub read the page makes. */
export const githubCooldowns = createGitHubCooldowns();
