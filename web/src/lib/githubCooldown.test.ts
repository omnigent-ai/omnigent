// The page waits out GitHub's rate limit as one client: one cooldown per reader, opened once
// for a burst of limited reads, doubled only by a limited read after it, and never shortened.

import { afterEach, describe, expect, it } from "vitest";

import {
  FALLBACK_COOLDOWN_MS,
  MAX_FALLBACK_COOLDOWN_MS,
  createGitHubCooldowns,
  discoverRequestCooldownKey,
  githubCooldownKey,
  isLocalRateLimitRefusal,
  requestCooldownKey,
  resolveAutomaticGitHubReaderWith,
} from "./githubCooldown";

const KEY = githubCooldownKey("github.com", "amy");

function harness({ random = () => 0 }: { random?: () => number } = {}) {
  let clock = 1_000_000;
  const timers: { at: number; callback: () => void; delay: number }[] = [];
  const cooldowns = createGitHubCooldowns({
    now: () => clock,
    random,
    setTimer: (callback, delay) => {
      const timer = { at: clock + delay, callback, delay };
      timers.push(timer);
      return timer as unknown as ReturnType<typeof setTimeout>;
    },
    clearTimer: (timer) => {
      const index = timers.indexOf(timer as unknown as (typeof timers)[number]);
      if (index >= 0) timers.splice(index, 1);
    },
  });
  /** Move the clock on, firing the timers that come due on the way. */
  const advance = (ms: number) => {
    const target = clock + ms;
    for (;;) {
      const due = timers.filter((timer) => timer.at <= target).sort((a, b) => a.at - b.at)[0];
      if (!due) break;
      timers.splice(timers.indexOf(due), 1);
      clock = due.at;
      due.callback();
    }
    clock = target;
  };
  return { cooldowns, advance, now: () => clock, timers };
}

describe("githubCooldowns", () => {
  it("opens one fallback cooldown for 20 simultaneous bare limits, and none of them extend it", () => {
    const { cooldowns, now } = harness({ random: () => 0.5 });
    const until = now() + FALLBACK_COOLDOWN_MS * 1.1;
    for (let row = 0; row < 20; row += 1) cooldowns.rateLimited(KEY, null);
    expect(cooldowns.heldUntil(KEY)).toBe(until);
    expect(cooldowns.phase(KEY)).toEqual({ phase: "waiting", until });
  });

  it("keeps late bare limits from moving the deadline while it waits", () => {
    const { cooldowns, advance, now } = harness();
    cooldowns.rateLimited(KEY, null);
    const until = cooldowns.heldUntil(KEY);
    for (let late = 0; late < 5; late += 1) {
      advance(10_000);
      cooldowns.rateLimited(KEY, null);
    }
    expect(cooldowns.heldUntil(KEY)).toBe(until);
    expect(until).toBe(now() - 50_000 + FALLBACK_COOLDOWN_MS);
  });

  it("lets a deadline the shell named extend the wait, never shorten it", () => {
    const { cooldowns, now } = harness();
    cooldowns.rateLimited(KEY, null);
    cooldowns.rateLimited(KEY, now() + 10_000);
    expect(cooldowns.heldUntil(KEY)).toBe(now() + FALLBACK_COOLDOWN_MS);
    cooldowns.rateLimited(KEY, now() + 2 * 60 * 60_000);
    expect(cooldowns.heldUntil(KEY)).toBe(now() + 2 * 60 * 60_000);
  });

  it("doubles the fallback once when the first read after it is limited again", () => {
    const { cooldowns, advance, now } = harness();
    cooldowns.rateLimited(KEY, null);
    advance(FALLBACK_COOLDOWN_MS);
    expect(cooldowns.phase(KEY)?.phase).toBe("recovering");
    // The recovery read and a row in flight with it: one doubling, not two.
    cooldowns.rateLimited(KEY, null);
    cooldowns.rateLimited(KEY, null);
    expect(cooldowns.heldUntil(KEY)).toBe(now() + 2 * FALLBACK_COOLDOWN_MS);
    for (let round = 0; round < 6; round += 1) {
      advance(MAX_FALLBACK_COOLDOWN_MS);
      cooldowns.rateLimited(KEY, null);
    }
    expect(cooldowns.heldUntil(KEY)).toBe(now() + MAX_FALLBACK_COOLDOWN_MS);
  });

  it.each([
    ["later", 10 * 60_000],
    ["earlier", 1_000],
  ])(
    "uses the shell's deadline for a limited recovery, %s than the doubled fallback",
    (_case, wait) => {
      const { cooldowns, advance, now } = harness();
      cooldowns.rateLimited(KEY, null);
      advance(FALLBACK_COOLDOWN_MS);
      // The doubled fallback would be 2 minutes; a deadline GitHub named wins either way.
      cooldowns.rateLimited(KEY, now() + wait);
      expect(cooldowns.heldUntil(KEY)).toBe(now() + wait);
    },
  );

  it("doubles only after a fallback wait, never after a deadline the shell named", () => {
    const { cooldowns, advance, now } = harness();
    // The shell names the first wait: the first fallback starts at its base.
    cooldowns.rateLimited(KEY, now() + 30_000);
    advance(30_000);
    cooldowns.rateLimited(KEY, null);
    expect(cooldowns.heldUntil(KEY)).toBe(now() + FALLBACK_COOLDOWN_MS);
    // A named wait between fallbacks doesn't double the next one either.
    advance(FALLBACK_COOLDOWN_MS);
    cooldowns.rateLimited(KEY, now() + 30_000);
    advance(30_000);
    cooldowns.rateLimited(KEY, null);
    expect(cooldowns.heldUntil(KEY)).toBe(now() + FALLBACK_COOLDOWN_MS);
    // That fallback proved too short: the next one doubles.
    advance(FALLBACK_COOLDOWN_MS);
    cooldowns.rateLimited(KEY, null);
    expect(cooldowns.heldUntil(KEY)).toBe(now() + 2 * FALLBACK_COOLDOWN_MS);
  });

  it("ends the cooldown at the first answer after it, and ignores answers while it waits", () => {
    const { cooldowns, advance, now } = harness();
    cooldowns.rateLimited(KEY, null);
    cooldowns.answered(KEY);
    expect(cooldowns.phase(KEY)?.phase).toBe("waiting");
    advance(FALLBACK_COOLDOWN_MS);
    cooldowns.answered(KEY);
    expect(cooldowns.phase(KEY)).toBeNull();
    // A new limit afterwards starts over at the first fallback.
    cooldowns.rateLimited(KEY, null);
    expect(cooldowns.heldUntil(KEY)).toBe(now() + FALLBACK_COOLDOWN_MS);
  });

  it("refuses reads while it waits with the deadline, marked as the page's own refusal", () => {
    const { cooldowns, now } = harness();
    expect(cooldowns.refusal(KEY)).toBeNull();
    cooldowns.rateLimited(KEY, now() + 30_000);
    const refusal = cooldowns.refusal(KEY);
    expect(refusal).toMatchObject({ code: "rate_limited", retryAt: now() + 30_000, local: true });
    expect(isLocalRateLimitRefusal(refusal)).toBe(true);
    expect(isLocalRateLimitRefusal({ code: "rate_limited", message: "x" })).toBe(false);
  });

  it("waits the fallback for a limit whose deadline already passed, so a read never repeats at once", () => {
    const { cooldowns, now } = harness();
    cooldowns.rateLimited(KEY, now() - 1);
    expect(cooldowns.heldUntil(KEY)).toBe(now() + FALLBACK_COOLDOWN_MS);
  });

  it("remembers the latest deadline after the cooldown ends", () => {
    const { cooldowns, advance, now } = harness();
    expect(cooldowns.lastDeadline(KEY)).toBeNull();
    cooldowns.rateLimited(KEY, now() + 4_000);
    const deadline = now() + 4_000;
    advance(4_000);
    cooldowns.answered(KEY);
    expect(cooldowns.phase(KEY)).toBeNull();
    expect(cooldowns.lastDeadline(KEY)).toBe(deadline);
  });

  it("tells subscribers when a wait ends, waking in chunks for a long one", () => {
    const { cooldowns, advance, timers, now } = harness();
    let heard = 0;
    cooldowns.subscribe(() => {
      heard += 1;
    });
    cooldowns.rateLimited(KEY, now() + 2 * 60 * 60_000);
    expect(heard).toBe(1);
    expect(timers[0]?.delay).toBe(10 * 60_000);
    advance(2 * 60 * 60_000 - 1);
    expect(heard).toBe(1);
    advance(1);
    expect(heard).toBe(2);
    expect(cooldowns.phase(KEY)?.phase).toBe("recovering");
  });

  it("says whether any reader on a host is waiting, only while one is", () => {
    const { cooldowns, advance } = harness();
    expect(cooldowns.holdsHost("github.com")).toBe(false);
    cooldowns.rateLimited(KEY, null);
    expect(cooldowns.holdsHost("GitHub.com")).toBe(true);
    expect(cooldowns.holdsHost("ghe.example.com")).toBe(false);
    advance(FALLBACK_COOLDOWN_MS);
    expect(cooldowns.holdsHost("github.com")).toBe(false);
  });

  it("keeps each host and reader apart", () => {
    const { cooldowns } = harness();
    cooldowns.rateLimited(KEY, null);
    expect(cooldowns.heldUntil(githubCooldownKey("github.com", "octo"))).toBeNull();
    expect(cooldowns.heldUntil(githubCooldownKey("ghe.example.com", "amy"))).toBeNull();
    expect(cooldowns.heldUntil(githubCooldownKey("GitHub.com", "AMY"))).not.toBeNull();
  });
});

describe("requestCooldownKey", () => {
  const unproven = { known: () => null, discover: async () => null };
  afterEach(() => resolveAutomaticGitHubReaderWith(unproven));
  const base = {
    provider: "github" as const,
    host: "github.com",
    operation: "pull_request_overview",
  };

  it("names the login a GitHub read goes out as, Automatic's once the shell proved it", () => {
    let proven: string | null = "amy";
    resolveAutomaticGitHubReaderWith({ known: () => proven, discover: async () => "bo" });
    // Automatic and a named read as the same account share one cooldown.
    expect(requestCooldownKey({ ...base, account: "mac" })).toBe(KEY);
    expect(requestCooldownKey({ ...base, account: "mac", macLogin: "Amy" })).toBe(KEY);
    expect(requestCooldownKey({ ...base, login: "host-account" })).toBe(
      githubCooldownKey("github.com", "host-account"),
    );
    // Not proven yet: nothing is known now, whatever the shell would say.
    proven = null;
    expect(requestCooldownKey({ ...base, account: "mac" })).toBeNull();
  });

  it("asks the shell who an unproven Automatic read goes out as", async () => {
    let discovered: string | null = "bo";
    resolveAutomaticGitHubReaderWith({ known: () => null, discover: async () => discovered });
    expect(await discoverRequestCooldownKey({ ...base, account: "mac" })).toBe(
      githubCooldownKey("github.com", "bo"),
    );
    // Signed out: nothing holds the read.
    discovered = null;
    expect(await discoverRequestCooldownKey({ ...base, account: "mac" })).toBeNull();
  });

  it("leaves out Example Host and the reads the shell answers without GitHub", async () => {
    resolveAutomaticGitHubReaderWith({ known: () => "amy", discover: async () => "amy" });
    const deviceHost = {
      provider: "named-host" as const,
      host: "2.host.example.com",
      operation: "x",
    };
    expect(requestCooldownKey(deviceHost)).toBeNull();
    expect(await discoverRequestCooldownKey(deviceHost)).toBeNull();
    for (const operation of [
      "account_list",
      "account_status",
      "organization_members",
      "recent_reviewers",
    ]) {
      const request = { provider: "github" as const, host: "github.com", operation };
      expect(requestCooldownKey(request)).toBeNull();
      // eslint-disable-next-line no-await-in-loop -- each operation is checked on its own.
      expect(await discoverRequestCooldownKey(request)).toBeNull();
    }
  });
});

describe("the limit a cooldown waits out", () => {
  const PRIMARY = {
    kind: "primary",
    resource: "graphql",
    resetAt: 1_000_000 + 18 * 60_000,
  } as const;

  it("keeps the limit the shell named, and only names one when it says something", () => {
    const { cooldowns, now } = harness();
    cooldowns.rateLimited(KEY, now() + 18 * 60_000, PRIMARY);
    expect(cooldowns.phase(KEY)).toEqual({
      phase: "waiting",
      until: now() + 18 * 60_000,
      limit: PRIMARY,
    });
    const other = githubCooldownKey("github.com", "ben");
    cooldowns.rateLimited(other, null, { kind: "unknown", resource: "unknown" });
    expect(cooldowns.phase(other)).not.toHaveProperty("limit");
  });

  it("lets a named limit replace one that said nothing within a wait, without moving the deadline", () => {
    const { cooldowns } = harness();
    cooldowns.rateLimited(KEY, null);
    const until = cooldowns.heldUntil(KEY);
    cooldowns.rateLimited(KEY, null, { kind: "secondary", resource: "graphql" });
    expect(cooldowns.phase(KEY)).toEqual({
      phase: "waiting",
      until,
      limit: { kind: "secondary", resource: "graphql" },
    });
    // A later answer naming nothing new leaves it as it was.
    cooldowns.rateLimited(KEY, null, { kind: "primary", resource: "core" });
    expect(cooldowns.phase(KEY)).toMatchObject({ limit: { kind: "secondary" } });
  });

  it("names the limit of a later deadline, and a new cooldown's own limit after a recovery", () => {
    const { cooldowns, advance, now } = harness();
    cooldowns.rateLimited(KEY, now() + 60_000, { kind: "secondary", resource: "graphql" });
    cooldowns.rateLimited(KEY, PRIMARY.resetAt, PRIMARY);
    expect(cooldowns.phase(KEY)).toMatchObject({ until: PRIMARY.resetAt, limit: PRIMARY });
    advance(PRIMARY.resetAt - now() + 1);
    cooldowns.rateLimited(KEY, null);
    expect(cooldowns.phase(KEY)).not.toHaveProperty("limit");
  });

  it("shows no earlier limit with a later deadline that names none", () => {
    const { cooldowns, now } = harness();
    cooldowns.rateLimited(KEY, now() + 60_000, { kind: "secondary", resource: "graphql" });
    const later = now() + 5 * 60_000;
    cooldowns.rateLimited(KEY, later);
    expect(cooldowns.phase(KEY)).toMatchObject({ until: later });
    expect(cooldowns.phase(KEY)).not.toHaveProperty("limit");
  });
});
