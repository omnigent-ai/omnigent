// Which path reads a pull request: this Mac, through the desktop shell as the
// Mac's own identity, or the session's host. The Mac reads a named host registered to
// read as the Mac whenever the shell can, and github.com or GitHub Enterprise when the shell picks Mac accounts
// and the user's setting allows it. Everything else, including browsers and
// mobile, uses the host. The store here also keeps the identity behind Mac reads
// so a cached answer never outlives a change of account or workspace user.
// The Mac path reads and writes linked GitHub data through the Mac's own gh, with no
// running session host, so a closed session still shows whether its pull request merged.

import { useSyncExternalStore } from "react";

import {
  BLOCKED_READ_SCOPE,
  decideReadRoute,
  pullRequestPageUrl,
  readChoiceKey,
  type ReadChoice,
  type ReadRoute,
  ReadSourceUnavailableError,
} from "@/lib/pullRequestReadRoute";
import {
  NO_REPO_READER,
  pullRequestRepository,
  savedRepoAccount,
  type RepoReaderState,
} from "@/lib/pullRequestRepoAccounts";

import {
  codeHostProvider,
  isDeviceReadProvider,
  isMacReadProvider as isMacReadProviderId,
  namedCodeHost,
  parseCodeHostPullRequestUrl,
  type CodeHostPullRequest,
  type CodeHostProviderId,
} from "@/lib/codeHosts";
import {
  type AutomaticFeatureIntent,
  createAutomaticFeaturePreference,
} from "@/lib/automaticFeaturePreference";
import { subscribeDesktopFeatureFlag } from "@/lib/desktopFeatureFlags";
import { resolveAutomaticGitHubReaderWith } from "@/lib/githubCooldown";
import { getCurrentUserId, subscribeIdentity } from "@/lib/identity";
import { acceptedWorkspaceDesign, subscribeAcceptedWorkspaceDesign } from "@/lib/workspaceSurfaces";
import {
  acceptMacRead,
  acceptedMacReader,
  advanceMacReadScope,
  beginMacRead,
  type MacReadProvider,
  type MacReadTicket,
  macNamedReadScopeKey,
  macReadCurrent,
  macReadScopeKey,
  type MacReadScopeKey,
  refuseMacReadSentAt,
  resetMacReadScopesForTests,
} from "@/lib/macReadScope";
import type { MacNamedReader } from "@/lib/macNamedReads";
import {
  type CodeHostProvenance,
  desktopCodeHostQuery,
  desktopMacAccounts,
  getDesktopFeatures,
  hasCodeHostAccountsBridge,
  hasCodeHostBridge,
  hasNamedCodeHostWriteBridge,
  onDesktopMacAccountsChanged,
  type DesktopCodeHost,
  type MacAccountChoice,
  type MacAccountIdentity,
} from "@/lib/nativeBridge";

/** What showing a PR in the panel needs from the shell, for any provider. */
export const PANEL_READ_OPERATIONS = [
  "pull_request_lookup",
  "pull_request_overview",
  "pull_request_files",
  "pull_request_patch",
  "pull_request_file_contents",
] as const;

/** GitHub also needs the Mac's account list, which says which hosts it's signed in to. */
export const GITHUB_MAC_READ_OPERATIONS = [...PANEL_READ_OPERATIONS, "account_list"] as const;

/** The remote flag Automatic follows for GitHub reads on this Mac. */
export const DESKTOP_CODE_HOST_READS_FLAG = "desktop-code-host-reads";

/** The user's choice for reading github.com and GitHub Enterprise PRs on this Mac. */
export const githubMacReadsPreference = createAutomaticFeaturePreference(
  "omnigent:desktop-code-host-reads",
);

export const GITHUB_MAC_READS_CHOICE_LABELS: Record<AutomaticFeatureIntent, string> = {
  automatic: "Automatic",
  on: "On",
  off: "Off",
};

/** Whether GitHub PRs are read on this Mac: On and Off override; Automatic follows the flag. */
export function githubMacReadsEnabled(
  intent: AutomaticFeatureIntent,
  automaticPolicy: boolean,
): boolean {
  if (intent === "on") return true;
  if (intent === "off") return false;
  return automaticPolicy;
}

/** The providers this shell reads PRs from on the Mac, from its `codeHosts` feature. */
export interface DesktopReadSupport {
  github: DesktopCodeHost | null;
  /** Every other host the shell offers, by id; only a registered device-read host uses one. */
  named: Readonly<Record<string, DesktopCodeHost>>;
}

export const NO_DESKTOP_READS: DesktopReadSupport = { github: null, named: {} };

const offers = (host: DesktopCodeHost | null, operations: readonly string[]) =>
  !!host && operations.every((operation) => host.operations.includes(operation));

/** Whether this shell can read `provider` PRs on the Mac at all, setting aside the user's choice. */
export function desktopReadsProvider(
  support: DesktopReadSupport,
  provider: CodeHostPullRequest["provider"],
): boolean {
  if (isDeviceReadProvider(provider)) {
    return offers(support.named[provider] ?? null, PANEL_READ_OPERATIONS);
  }
  if (provider === "github") {
    return support.github?.accounts === true && offers(support.github, GITHUB_MAC_READ_OPERATIONS);
  }
  return false;
}

/**
 * The PR `url` names when this Mac reads it, else null for the host path.
 * `githubHosts` are hosts known to serve GitHub Enterprise.
 */
export function classifyPullRequestRead(
  url: string | null | undefined,
  {
    support,
    githubReadsEnabled,
    githubHosts = [],
  }: {
    support: DesktopReadSupport;
    githubReadsEnabled: boolean;
    githubHosts?: Iterable<string>;
  },
): CodeHostPullRequest | null {
  const target = parseCodeHostPullRequestUrl(url, githubHosts);
  if (!target || !desktopReadsProvider(support, target.provider)) return null;
  if (target.provider === "github" && !githubReadsEnabled) return null;
  return target;
}

/** A shell's advertised writes, kept only when well formed; the write path checks the set. */
function writesFrom(writes: unknown): DesktopCodeHost["writes"] {
  if (!writes || typeof writes !== "object") return undefined;
  const { version, operations, runGrants } = writes as {
    version?: unknown;
    operations?: unknown;
    runGrants?: unknown;
  };
  if (!Number.isInteger(version) || !Array.isArray(operations)) return undefined;
  return {
    version: version as number,
    operations: operations.filter(
      (operation): operation is string => typeof operation === "string",
    ),
    // One click may authorize a bulk run's writes; absent on older shells.
    ...(Number.isInteger(runGrants) ? { runGrants: runGrants as number } : {}),
  };
}

function supportFrom(codeHosts: unknown): DesktopReadSupport {
  const hosts = Array.isArray(codeHosts) ? (codeHosts as Partial<DesktopCodeHost>[]) : [];
  const entry = (id: string): DesktopCodeHost | null => {
    const host = hosts.find((candidate) => candidate?.id === id);
    if (!host || !Array.isArray(host.operations)) return null;
    return {
      id: host.id as DesktopCodeHost["id"],
      operations: host.operations.filter((operation) => typeof operation === "string"),
      accounts: host.accounts === true,
      sign_in: host.sign_in === true,
      answerProvenance: host.answerProvenance === 1 ? 1 : undefined,
      readAs: host.readAs === 1 ? 1 : undefined,
      writeAs: host.writeAs === 1 ? 1 : undefined,
      writes: writesFrom(host.writes),
    };
  };
  const github = entry("github");
  return {
    // A shell that lists accounts without the bridge to pick them can't read GitHub as the Mac.
    github: github && (!github.accounts || hasCodeHostAccountsBridge()) ? github : null,
    named: Object.fromEntries(
      hosts.flatMap((host) => {
        const id = host?.id;
        const offered = typeof id === "string" && id !== "github" ? entry(id) : null;
        return offered ? [[id, offered] as const] : [];
      }),
    ),
  };
}

// ---------------------------------------------------------------------------
// The live store: shell support, the setting, known Enterprise hosts, and the
// identity per read scope. `version` moves on every change, so hooks re-render.
// Mac reads are cached and ordered under the shared scope in `macReadScope.ts`;
// this store adds what main reports about GitHub accounts, and advances a scope
// (dropping its queries) when the account behind it changes.

const HOST_NAME =
  /^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$/;

type Provider = CodeHostPullRequest["provider"];

/** What this store knows about one scope beyond its order and reader. */
interface ScopeState {
  provider: MacReadProvider;
  host: string;
  /** GitHub: the last account a read or the shell reported, if any. */
  identity: Pick<MacAccountIdentity, "login" | "generation"> | null;
  /** GitHub: the oldest generation an answer may carry, before any answer was kept. */
  generationFloor: number;
  /** A read failed since the last kept answer, so a change must read it again. */
  failed: boolean;
}

let support: DesktopReadSupport | null = null;
let supportRequest: Promise<DesktopReadSupport> | null = null;
let automaticPolicy = false;
const githubHosts = new Set<string>();
const probedHosts = new Map<string, Promise<boolean>>();
// Hosts this Mac's gh isn't signed in to, until the shell says their accounts changed.
const unsignedHosts = new Set<string>();
// GitHub hosts whose account main is being asked for again, after a change dropped it.
const identityChecks = new Set<string>();
const scopes = new Map<string, ScopeState>();
// The workspace user the scopes' reads were for, so a switch drops that user's.
let scopeUser: string | null = null;
// Each PR's explicit read choice for this app run, by canonical URL; absent is Automatic.
const readChoices = new Map<string, Exclude<ReadChoice, "automatic">>();
// Sessions whose branch PR the user asked the session's machine to find, for this run.
const sessionMachineBranches = new Set<string>();
const listeners = new Set<() => void>();
let version = 0;
let started = false;
/** Ends what `start` listens to, so a test's reset leaves no subscription behind. */
let stopWatching: (() => void) | null = null;

function changed(): void {
  version += 1;
  for (const listener of listeners) listener();
}

function scopeState(provider: MacReadProvider, host: string): ScopeState {
  const key = `${provider}\n${host.toLowerCase()}`;
  let entry = scopes.get(key);
  if (!entry) {
    entry = {
      provider,
      host: host.toLowerCase(),
      identity: null,
      generationFloor: 0,
      failed: false,
    };
    scopes.set(key, entry);
  }
  return entry;
}

/** GitHub's scope for `host`; account events and choices are GitHub's only. */
const githubScope = (host: string) => scopeState("github", host);

/**
 * The identity behind `entry` changed: refuse every read sent before now, and
 * drop the scope's queries so nothing read as the previous identity shows.
 */
function advance(entry: ScopeState): void {
  entry.failed = false;
  advanceMacReadScope(macReadScopeKey(entry.provider, entry.host));
  changed();
}

/**
 * The shell said `host`'s accounts or the choice among them changed. The same
 * generation as the account last read means only the choice did: what's shown
 * re-reads, and answers already read as that account stay.
 */
function accountsChanged(host: string, generation: number | null): void {
  const entry = githubScope(host);
  const known = entry.identity;
  if (generation !== null && known?.generation === generation) {
    unsignedHosts.delete(host);
    changed();
    return;
  }
  // Before any answer was kept, nothing shown predates the change, and main refuses
  // reads that outlived their generation: answers as this generation or newer stay.
  // A read that already failed, e.g. signed out, is read again under a new scope.
  if (generation !== null && !known && !entry.failed) {
    unsignedHosts.delete(host);
    bumpAccountEpoch(host);
    entry.generationFloor = Math.max(entry.generationFloor, generation);
    changed();
    return;
  }
  accountChanged(host);
}

/**
 * Ask main which account it reads `host` as when no read has said yet, e.g. a Pull Requests
 * page that reads as its own login and so never reads as the host's: writes act as that
 * account and need it named. Nothing read moves.
 */
export function ensureMacIdentity(host: string): void {
  const name = host.toLowerCase();
  const entry = githubScope(name);
  // A check still out for the previous workspace user doesn't answer for this one.
  if (entry.identity) return;
  if (identityChecks.has(name) && identityCheckUsers.get(name) === getCurrentUserId()) return;
  resolveMacIdentity(name, entry);
  changed();
}

/** Re-read the account `host` is read as, after the shell says it changed. */
function accountChanged(host: string): void {
  // A sign-in to an Enterprise host this Mac couldn't read makes it readable.
  unsignedHosts.delete(host);
  const entry = githubScope(host);
  entry.identity = null;
  bumpAccountEpoch(host);
  resolveMacIdentity(host.toLowerCase(), entry);
  advance(entry);
}

// Counts the account changes the shell reported per host, so a lookup that outlived one
// names nobody rather than the account after it.
const accountEpochs = new Map<string, number>();
// Moves on every test reset, so a lookup still out from before it names nobody after it.
let readPathLifetime = 0;

function bumpAccountEpoch(host: string): void {
  const name = host.toLowerCase();
  accountEpochs.set(name, (accountEpochs.get(name) ?? 0) + 1);
}

// The latest identity request per host, so only its answer settles the host's account, and
// the workspace user it was asked for.
const identityRequests = new Map<string, number>();
// Settles when the latest account request per host has answered, whatever it said.
const identityResolutions = new Map<string, Promise<void>>();
const identityCheckUsers = new Map<string, string | null>();
// A main that keeps naming an account older than its own change event is asked no further.
const MAX_STALE_IDENTITY_ANSWERS = 3;

/**
 * Ask main for `name`'s account and keep it only if, when it answers, it's still the latest
 * request, for the same workspace user, at or past the oldest generation main has reported.
 * An answer older than that asks again.
 */
function resolveMacIdentity(name: string, entry: ScopeState, staleAnswers = 0): void {
  const request = (identityRequests.get(name) ?? 0) + 1;
  identityRequests.set(name, request);
  const userId = getCurrentUserId();
  identityChecks.add(name);
  identityCheckUsers.set(name, userId);
  const resolution = desktopMacAccounts(name).then((choice) => {
    if (identityRequests.get(name) !== request) return;
    if (userId !== getCurrentUserId()) {
      identityChecks.delete(name);
      changed();
      return;
    }
    const effective = choice?.effective;
    const stale = effective && effective.generation < entry.generationFloor;
    if (stale && !entry.identity && staleAnswers < MAX_STALE_IDENTITY_ANSWERS) {
      // Read before a change main has since reported: ask again for the account after it.
      resolveMacIdentity(name, entry, staleAnswers + 1);
      return;
    }
    identityChecks.delete(name);
    if (effective && !stale && !entry.identity) entry.identity = effective;
    changed();
  });
  identityResolutions.set(name, resolution);
}

function start(): void {
  if (started) return;
  started = true;
  const stopReadsFlag = subscribeDesktopFeatureFlag(
    DESKTOP_CODE_HOST_READS_FLAG,
    { fallback: false, mode: "resolved" },
    ({ value }) => {
      if (value === automaticPolicy) return;
      automaticPolicy = value === true;
      changed();
    },
  );
  const stopPreference = githubMacReadsPreference.subscribe(changed);
  // Classic can't show or undo "This Mac", so its reads set the choice aside and re-route.
  let classic = acceptedWorkspaceDesign() === "classic";
  const stopDesign = subscribeAcceptedWorkspaceDesign(() => {
    const next = acceptedWorkspaceDesign() === "classic";
    if (next === classic) return;
    classic = next;
    changed();
  });
  const stopAccounts = onDesktopMacAccountsChanged(accountsChanged);
  // Another workspace user's answers must not show: keys carry the user, and the
  // shared scope advances every one of the previous user's. What main said about
  // accounts was for them too.
  scopeUser = getCurrentUserId();
  const stopIdentity = subscribeIdentity(() => {
    const previous = scopeUser;
    scopeUser = getCurrentUserId();
    if (previous === scopeUser) return;
    for (const entry of scopes.values()) {
      entry.identity = null;
      entry.failed = false;
    }
    // A choice was the previous user's; the next one starts on Automatic.
    readChoices.clear();
    changed();
  });
  stopWatching = () => {
    stopReadsFlag();
    stopPreference();
    stopDesign();
    stopAccounts();
    stopIdentity();
  };
  // The repository choices' policy, loaded apart since it reads named reads, which read this.
  if (!repoPolicy) void import("@/lib/pullRequestRepoReader");
}

/** Who reads each repository's pull requests, installed by pullRequestRepoReader. */
export interface RepoReaderPolicy {
  /** Who `url`'s repository is read as now, given the PR's own read choice. */
  readerFor: (url: string) => RepoReaderState;
  /** `url`'s read choice just changed, before anything re-reads: retire a reader it left. */
  readChoiceChanged: (url: string) => void;
}

// Until the policy is installed, a saved login is checked, never read as another account.
let repoPolicy: RepoReaderPolicy | null = null;

/** Route PRs by their repository's choice from now on; the policy says who reads each. */
export function installRepoReaderPolicy(policy: RepoReaderPolicy): void {
  repoPolicy = policy;
  changed();
}

/** A repository's choice, or whether it can be used, changed: routes are decided again. */
export function repoReaderPolicyChanged(): void {
  changed();
}

/** Who `url`'s repository is read as on this Mac; see {@link RepoReaderState}. */
export function repoReaderFor(rawUrl: string | null | undefined): RepoReaderState {
  const url = pullRequestPageUrl(rawUrl);
  if (!url) return NO_REPO_READER;
  if (repoPolicy) return repoPolicy.readerFor(url);
  const repository = pullRequestRepository(url);
  if (!repository) return NO_REPO_READER;
  const saved = savedRepoAccount(repository);
  if (saved.kind === "pending") return { kind: "blocked", reason: "pending", reader: null };
  return saved.intent.mode === "account"
    ? {
        kind: "blocked",
        reason: "checking",
        reader: { host: repository.host, login: saved.intent.login },
      }
    : NO_REPO_READER;
}

/** The repository's chosen login where it reads as one now; null otherwise. */
function repoNamedReader(url: string | null | undefined): MacNamedReader | null {
  const state = repoReaderFor(url);
  return state.kind === "named" ? state.reader : null;
}

function loadSupport(): Promise<DesktopReadSupport> {
  start();
  if (support) return Promise.resolve(support);
  if (!hasCodeHostBridge()) {
    support = NO_DESKTOP_READS;
    return Promise.resolve(support);
  }
  supportRequest ??= getDesktopFeatures().then(
    (features) => {
      support = supportFrom(features?.codeHosts);
      changed();
      return support;
    },
    () => {
      support = NO_DESKTOP_READS;
      changed();
      return support;
    },
  );
  return supportRequest;
}

// Each refresh is numbered, so a slower, older answer can't replace a newer one.
let writesRefresh = 0;

/**
 * Ask the shell again which writes and named reads it advertises, e.g. after a release flag
 * changed, and keep only those: what this Mac reads stays as first answered. A failed ask
 * keeps what was known.
 */
export async function refreshDesktopReadSupport(): Promise<void> {
  if (!hasCodeHostBridge()) return;
  const known = await loadSupport();
  const request = ++writesRefresh;
  const features = await getDesktopFeatures();
  if (request !== writesRefresh || !features || support !== known || !known.github) return;
  const fresh = supportFrom(features.codeHosts).github;
  const writes = fresh?.writes;
  const readAs = fresh?.readAs;
  const writeAs = fresh?.writeAs;
  if (
    JSON.stringify(writes) === JSON.stringify(known.github.writes) &&
    readAs === known.github.readAs &&
    writeAs === known.github.writeAs
  ) {
    return;
  }
  support = { ...known, github: { ...known.github, writes, readAs, writeAs } };
  changed();
}

/** Whether this shell reads GitHub as a login a page names (`macLogin`), as last answered. */
export function macNamedReadsOffered(): boolean {
  // Named reads come with this Mac's accounts, which list who can be named.
  return support?.github?.readAs === 1 && support.github.accounts === true;
}

/**
 * Whether this shell makes changes as a login a page reads as by name, as last answered: the
 * capability and its preload method both, so a partial shell never gets a named write.
 */
export function macNamedWritesOffered(): boolean {
  return macNamedReadsOffered() && support?.github?.writeAs === 1 && hasNamedCodeHostWriteBridge();
}

function githubReadsEnabledNow(): boolean {
  return githubMacReadsEnabled(githubMacReadsPreference.read(), automaticPolicy);
}

// Each PR's route as of the store's version: a long list asks for every row's route several
// times a render, and working one out parses the URL each time.
let routeCache: {
  version: number;
  support: DesktopReadSupport | null;
  routes: Map<string, ReadRoute>;
} | null = null;

/** The PR's effective read route now, from the automatic policy and the user's choice. */
export function readRouteFor(rawUrl: string | null | undefined): ReadRoute {
  // Before the store listens, a change to the setting wouldn't move its version.
  if (!rawUrl || !started) return routeNow(rawUrl);
  if (routeCache?.version !== version || routeCache.support !== support) {
    routeCache = { version, support, routes: new Map() };
  }
  let route = routeCache.routes.get(rawUrl);
  if (!route) {
    route = routeNow(rawUrl);
    routeCache.routes.set(rawUrl, route);
  }
  return route;
}

function routeNow(rawUrl: string | null | undefined): ReadRoute {
  // Every page of a PR routes as the PR itself, so a subpage can't slip past a choice.
  const url = pullRequestPageUrl(rawUrl) ?? rawUrl;
  const choice = appliedReadChoiceFor(url);
  // A repository's chosen login reads it on this Mac as that login, or nobody reads it. Only
  // Session's machine chosen for the PR itself sets it aside, never Classic's reading of This Mac.
  const repo = readChoiceFor(url) === "session" ? NO_REPO_READER : repoReaderFor(url);
  if (repo.kind === "blocked") return { kind: "blocked", reason: "repo_account" };
  if (repo.kind === "named") {
    const forced = support
      ? classifyPullRequestRead(url, { support, githubReadsEnabled: true, githubHosts })
      : null;
    return forced ? { kind: "mac", target: forced } : { kind: "blocked", reason: "repo_account" };
  }
  if (!support) {
    // Until the shell answers, nothing reads on this Mac; an explicit Mac choice still waits.
    return choice === "mac" ? { kind: "blocked", reason: "mac_unsupported" } : { kind: "host" };
  }
  return decideReadRoute(choice, {
    automatic: choice === "automatic" ? classifyIgnoringChoice(url) : null,
    forced:
      choice === "mac"
        ? classifyPullRequestRead(url, { support, githubReadsEnabled: true, githubHosts })
        : null,
  });
}

/**
 * The PR as this Mac could read it, whatever the route, choice, Settings or policy: its
 * provider and host once this Mac supports the provider's whole read contract and knows
 * the host. Null before the shell answers and where this Mac can't read it at all.
 */
export function macReadCandidate(rawUrl: string | null | undefined): CodeHostPullRequest | null {
  if (!support) return null;
  const url = pullRequestPageUrl(rawUrl) ?? rawUrl;
  return classifyPullRequestRead(url, { support, githubReadsEnabled: true, githubHosts });
}

/** What Automatic would pick for `url` now, whatever the user chose. */
export function automaticReadRoute(url: string | null | undefined): "mac" | "host" {
  return classifyIgnoringChoice(pullRequestPageUrl(url) ?? url) ? "mac" : "host";
}

/** The user's choice for `url`; Automatic when there is none. */
export function readChoiceFor(url: string | null | undefined): ReadChoice {
  const key = readChoiceKey(url);
  return (key ? readChoices.get(key) : undefined) ?? "automatic";
}

/**
 * The choice `url`'s reads follow. Classic keeps a "This Mac" choice but reads through the
 * session's machine, since only the new pull request page shows or undoes it; Tabs applies it.
 */
function appliedReadChoiceFor(url: string | null | undefined): ReadChoice {
  const choice = readChoiceFor(url);
  return choice === "mac" && acceptedWorkspaceDesign() === "classic" ? "session" : choice;
}

/** Choose where `url` is read for the rest of this app run; Automatic forgets the choice. */
export function chooseReadSource(url: string, choice: ReadChoice): void {
  start();
  const key = readChoiceKey(url);
  if (!key || readChoiceFor(key) === choice) return;
  if (choice === "automatic") readChoices.delete(key);
  else readChoices.set(key, choice);
  repoPolicy?.readChoiceChanged(key);
  changed();
}

/**
 * Throw before asking the session's machine about `url` while its route is blocked, so a
 * PR the user chose to read on this Mac never shows the host's answer.
 */
export function assertHostReadAllowed(url: string | null | undefined): void {
  if (url && readRouteFor(url).kind === "blocked") throw new ReadSourceUnavailableError(url);
}

function classifyNow(url: string | null | undefined): CodeHostPullRequest | null {
  const route = readRouteFor(url);
  return route.kind === "mac" ? route.target : null;
}

/** What this Mac could read, setting aside a PR the user sent to the session's machine. */
function classifyIgnoringChoice(url: string | null | undefined): CodeHostPullRequest | null {
  if (!support) return null;
  return classifyPullRequestRead(url, {
    support,
    githubReadsEnabled: githubReadsEnabledNow(),
    githubHosts,
  });
}

/** Whether the user sent `url` back to the session's machine, which this Mac could read. */
export function readsOnSessionMachine(url: string | null | undefined): boolean {
  return readChoiceFor(url) === "session";
}

/** Read `url` on this Mac again, undoing {@link readOnSessionMachine}. */
export function readOnThisMacAgain(url: string): void {
  if (readChoiceFor(url) === "session") chooseReadSource(url, "automatic");
}

/** Read `url` through the session's machine for the rest of this app run. */
export function readOnSessionMachine(url: string): void {
  chooseReadSource(url, "session");
}

/** The operation that finds a session branch's pull request on this Mac. */
export const BRANCH_PULL_REQUESTS_OPERATION = "branch_pull_requests";

/**
 * The provider this Mac finds `host`'s branch PRs with, as the store knows now,
 * under the same setting, rollout and Enterprise evidence as a PR read; null
 * when the session's machine finds them, including after the user chose it.
 */
export function branchDiscoveryProvider(
  sessionId: string,
  host: string | null,
): CodeHostPullRequest["provider"] | null {
  if (!host || sessionMachineBranches.has(sessionId)) return null;
  const name = host.toLowerCase();
  const provider = codeHostProvider(name, githubHosts) ?? null;
  if (!provider) return null;
  return readsHostOnThisMac(provider, name, BRANCH_PULL_REQUESTS_OPERATION) ? provider : null;
}

/**
 * Whether {@link branchDiscoveryProvider} is settled for `host`: an Enterprise
 * host nothing vouched for yet is asked about here, and the store changes once
 * this Mac's gh answers.
 */
export function branchDiscoverySettled(host: string | null): boolean {
  if (!desktopReadSupportKnown()) {
    void loadSupport();
    return false;
  }
  if (!host || !support) return true;
  const name = host.toLowerCase();
  if (codeHostProvider(name, githubHosts)) return true;
  if (!desktopReadsProvider(support, "github") || !githubReadsEnabledNow()) return true;
  if (!HOST_NAME.test(name) || unsignedHosts.has(name)) return true;
  void probeGitHubHost(name);
  return false;
}

/**
 * The provider `host` is known to be: a named host read as the Mac, github.com, or an
 * Enterprise host with evidence.
 */
export function knownCodeHostProvider(host: string): CodeHostProviderId | null {
  const provider = codeHostProvider(host.toLowerCase(), githubHosts);
  return provider && isMacReadProviderId(provider) ? provider : null;
}

/**
 * The provider a checkout's remote host serves, "pending" while this Mac is still
 * asking its gh about an Enterprise host, or null when its links aren't known.
 */
export function contextHostProvider(host: string): CodeHostProviderId | "pending" | null {
  return knownCodeHostProvider(host) ?? (branchDiscoverySettled(host) ? null : "pending");
}

/** Whether the user asked the session's machine to find this session's branch PR. */
export function findsBranchOnSessionMachine(sessionId: string): boolean {
  return sessionMachineBranches.has(sessionId);
}

/** Find the session's branch PR with the session's machine for the rest of this app run. */
export function findBranchOnSessionMachine(sessionId: string): void {
  if (sessionMachineBranches.has(sessionId)) return;
  sessionMachineBranches.add(sessionId);
  changed();
}

/** Find the session's branch PR on this Mac again, undoing {@link findBranchOnSessionMachine}. */
export function findBranchOnThisMacAgain(sessionId: string): void {
  if (sessionMachineBranches.delete(sessionId)) changed();
}

/** Hosts the session's host reports serving GitHub, e.g. an Enterprise host its PRs are on. */
export function rememberGitHubHosts(hosts: Iterable<string>): void {
  let added = false;
  for (const host of hosts) {
    const name = host.toLowerCase();
    if (!HOST_NAME.test(name) || githubHosts.has(name)) continue;
    githubHosts.add(name);
    added = true;
  }
  if (added) changed();
}

/** Whether this Mac's gh is signed in to `host`, which shows it serves GitHub. */
function probeGitHubHost(host: string): Promise<boolean> {
  let probe = probedHosts.get(host);
  if (!probe) {
    probe = desktopCodeHostQuery<unknown[]>({
      provider: "github",
      host,
      operation: "account_list",
      params: {},
    }).then((result) => {
      const signedIn = result.ok && Array.isArray(result.data) && result.data.length > 0;
      probedHosts.delete(host);
      if (signedIn) rememberGitHubHosts([host]);
      else {
        unsignedHosts.add(host);
        changed();
      }
      return signedIn;
    });
    probedHosts.set(host, probe);
  }
  return probe;
}

/**
 * The PR `url` names when this Mac reads it, else null. An unrecognized host
 * counts as GitHub Enterprise once this Mac's gh is signed in to it.
 */
export async function desktopReadTarget(
  url: string | null | undefined,
): Promise<CodeHostPullRequest | null> {
  await loadSupport();
  const host = unprobedGitHubHost(url);
  if (host === null) return classifyNow(url);
  return (await probeGitHubHost(host)) ? classifyNow(url) : null;
}

/** The host of `url` when only asking this Mac's gh can tell whether it serves GitHub. */
function unprobedGitHubHost(rawUrl: string | null | undefined): string | null {
  // Any page of a PR probes as the PR itself, as its route does.
  const url = pullRequestPageUrl(rawUrl) ?? rawUrl;
  // A host already known, including for a PR sent to the session's machine, is settled,
  // whatever the policy says: an explicit "This Mac" reads a known host without a probe.
  if (!support || !url) return null;
  if (classifyPullRequestRead(url, { support, githubReadsEnabled: true, githubHosts })) return null;
  // An explicit "This Mac" may find an Enterprise host even where Settings leave it off.
  const mayRead = githubReadsEnabledNow() || appliedReadChoiceFor(url) === "mac";
  if (!desktopReadsProvider(support, "github") || !mayRead) return null;
  let host: string;
  try {
    host = new URL(url).hostname.toLowerCase();
  } catch {
    return null;
  }
  if (!HOST_NAME.test(host) || unsignedHosts.has(host)) return null;
  return parseCodeHostPullRequestUrl(url, [host]) === null ? null : host;
}

/**
 * Whether the path `url` is read on is settled, so a query can pick its key and
 * reader together. An Enterprise host nothing has vouched for yet is asked about
 * here, whether or not the session's machine is online; the store changes once it answers.
 */
export function readPathSettled(url: string | null | undefined): boolean {
  if (!url) return true;
  if (!desktopReadSupportKnown()) {
    void loadSupport();
    return false;
  }
  const host = unprobedGitHubHost(url);
  if (host === null) return true;
  void probeGitHubHost(host);
  return false;
}

/** The PR `url` names when this Mac reads it, as the store knows now; null otherwise. */
export function macReadTarget(url: string | null | undefined): CodeHostPullRequest | null {
  return classifyNow(url);
}

/** Whether `url` is read on this Mac, as the store knows now; false until the shell answers. */
export function readsOnThisMac(url: string | null | undefined): boolean {
  return classifyNow(url) !== null;
}

/**
 * The query-key part that keeps a PR's answers apart by read path, and on this
 * Mac by workspace user, provider and host. Empty for the host path, whose keys
 * stay as they were.
 */
export function readScope(
  url: string | null | undefined,
  readAs: MacNamedReader | null = null,
): readonly unknown[] {
  const route = readRouteFor(url);
  if (route.kind === "blocked") return BLOCKED_READ_SCOPE;
  const target = route.kind === "mac" ? route.target : null;
  // Read as the page's own login, else the repository's: its own scope, apart from the host's.
  const reader = readAs ?? repoNamedReader(url);
  if (target && readsAsNamed(target, reader)) {
    return macNamedReadScopeKey(target.host, reader!.login);
  }
  return target && isMacReadProvider(target.provider)
    ? macReadScopeKey(target.provider, target.host)
    : [];
}

/** Whether `target` reads as `readAs`: a GitHub PR on its host, while named reads are offered. */
function readsAsNamed(target: CodeHostPullRequest, readAs: MacNamedReader | null): boolean {
  return (
    !!readAs &&
    target.provider === "github" &&
    target.host.toLowerCase() === readAs.host.toLowerCase() &&
    macNamedReadsOffered()
  );
}

/** {@link macReadTarget}, naming `readAs` where the PR reads as that login. */
export function macReadTargetAs(
  url: string | null | undefined,
  readAs: MacNamedReader | null,
): CodeHostPullRequest | null {
  const target = classifyNow(url);
  const reader = readAs ?? repoNamedReader(url);
  return target && readsAsNamed(target, reader) ? { ...target, readAs: reader!.login } : target;
}

/**
 * The login this Mac reads `url` as, on the PR's own provider and host: the page's named
 * reader where it reads that host by name, else the reader the host's scope last accepted
 * (a device-read host's proven viewer, or GitHub's account). Null off this Mac or before a read says.
 */
export function macViewerLogin(
  url: string | null | undefined,
  readAs: MacNamedReader | null = null,
): string | null {
  const target = macReadTargetAs(url, readAs);
  if (!target) return null;
  if (target.readAs) return target.readAs;
  if (!isMacReadProvider(target.provider)) return null;
  const reader = acceptedMacReader(macReadScopeKey(target.provider, target.host));
  if (reader) return reader;
  return target.provider === "github" ? (macIdentity(target.host)?.login ?? null) : null;
}

/**
 * The query-key part a read the session's machine answers adds while the PR's route is
 * blocked, and nothing otherwise, so a host key never matches a Mac read's key.
 */
export function blockedReadScope(url: string | null | undefined): readonly unknown[] {
  return readRouteFor(url).kind === "blocked" ? BLOCKED_READ_SCOPE : [];
}

const isMacReadProvider = (provider: Provider): provider is MacReadProvider =>
  isMacReadProviderId(provider);

/** The account this Mac last read `host` as, or null before its first read. */
export function macIdentity(host: string): Pick<MacAccountIdentity, "login" | "generation"> | null {
  return scopes.get(`github\n${host.toLowerCase()}`)?.identity ?? null;
}

/**
 * The account this Mac reads `host` as now, asking main for it first when no read has proven
 * one yet; null when main names none, e.g. signed out, or the account changed meanwhile, so a
 * read sent now is never put down to the account after it.
 */
export async function discoverMacIdentity(host: string): Promise<string | null> {
  const name = host.toLowerCase();
  const known = macIdentity(name);
  if (known) return known.login;
  const epoch = accountEpochs.get(name) ?? 0;
  const lifetime = readPathLifetime;
  const userId = getCurrentUserId();
  // Main is asked once per account change: a check that named nobody answers until the next.
  const asked = identityResolutions.get(name);
  if (!asked || identityCheckUsers.get(name) !== getCurrentUserId()) ensureMacIdentity(name);
  await latestIdentityAnswer(name);
  const moved = (accountEpochs.get(name) ?? 0) !== epoch || readPathLifetime !== lifetime;
  if (moved || getCurrentUserId() !== userId) return null;
  return macIdentity(name)?.login ?? null;
}

/** Settles once the latest account request for `name` answered; a stale answer asks again. */
async function latestIdentityAnswer(name: string): Promise<void> {
  const pending = identityResolutions.get(name);
  if (!pending) return;
  await pending;
  if (identityResolutions.get(name) !== pending) await latestIdentityAnswer(name);
}

// Automatic reads wait out a rate limit as the account the shell proved, never a host-wide budget.
resolveAutomaticGitHubReaderWith({
  known: (host) => macIdentity(host)?.login ?? null,
  discover: discoverMacIdentity,
});

/** Whether the account for `host` is being asked for again, so its absence isn't settled yet. */
export function macIdentityChecking(host: string): boolean {
  return identityChecks.has(host.toLowerCase());
}

/** What a Mac read captured when it was sent, to check its answer against. */
export interface MacAnswerTicket {
  provider: MacReadProvider;
  host: string;
  userId: string | null;
  /** The read's place in its scope's order. */
  read: MacReadTicket;
}

export function macReadTicket(provider: MacReadProvider, host: string): MacAnswerTicket {
  const key = macReadScopeKey(provider, host);
  return { provider, host: key[3], userId: key[1], read: beginMacRead(key) };
}

/** Whether this shell's Mac answers for `provider` carry provenance to check. */
export function macAnswersCarryProvenance(provider: MacReadProvider): boolean {
  const entry = provider === "github" ? support?.github : support?.named[provider];
  return entry?.answerProvenance === 1;
}

/**
 * The viewer an answer `acceptMacAnswer` accepted was read as, by the same rule it checked: the
 * proven viewer where this shell proves answers, else main's account on GitHub. Never the
 * scope's current reader, which may have moved since.
 */
export function macAnswerViewer(
  ticket: MacAnswerTicket,
  answer: { identity?: MacAccountIdentity; provenance?: CodeHostProvenance },
): string | null {
  if (macAnswersCarryProvenance(ticket.provider)) {
    return validProvenance(answer.provenance, ticket) ? answer.provenance.viewer : null;
  }
  return ticket.provider === "github" ? (answer.identity?.login ?? null) : null;
}

/** Why an answer was refused: the scope moved and it reruns, or the answer can't be trusted. */
export type MacAnswerVerdict = "accepted" | "moved" | "unproven";

const sameLogin = (left: string, right: string) => left.toLowerCase() === right.toLowerCase();

function validProvenance(
  provenance: CodeHostProvenance | undefined,
  ticket: MacAnswerTicket,
): provenance is CodeHostProvenance {
  return (
    !!provenance &&
    provenance.version === 1 &&
    provenance.provider === ticket.provider &&
    typeof provenance.host === "string" &&
    provenance.host.toLowerCase() === ticket.host &&
    typeof provenance.viewer === "string" &&
    provenance.viewer.length > 0
  );
}

/**
 * Whether an answer read under `ticket` may be kept. It must come from the same
 * workspace user and not predate a change of the scope's reader; on GitHub, as
 * the account the scope shows, at or past the oldest generation main reported;
 * and, from shells that prove it, as a viewer the shared scope accepts. A newer
 * answer from another viewer advances the scope, so its queries read again.
 */
export function acceptMacAnswer(
  ticket: MacAnswerTicket,
  answer: { identity?: MacAccountIdentity; provenance?: CodeHostProvenance },
): MacAnswerVerdict {
  if (!ticketCurrent(ticket)) return "moved";
  const identity = ticket.provider === "github" ? answer.identity : undefined;
  const proven = macAnswersCarryProvenance(ticket.provider);
  if (proven && !validProvenance(answer.provenance, ticket)) {
    scopeState(ticket.provider, ticket.host).failed = true;
    return "unproven";
  }
  return acceptAnswerAs(ticket, identity, proven ? answer.provenance!.viewer : null);
}

/**
 * Whether an answer saved on this device as `viewer` may show again: the account
 * main resolves now, read under `ticket`, is that viewer, and the scope keeps it
 * as its reader, so a later account change drops what's restored.
 */
export function acceptRestoredMacAnswer(
  ticket: MacAnswerTicket,
  identity: MacAccountIdentity,
  viewer: string,
): boolean {
  if (ticket.provider !== "github" || !ticketCurrent(ticket)) return false;
  if (!sameLogin(identity.login, viewer)) return false;
  return acceptAnswerAs(ticket, identity, null, { restored: true }) === "accepted";
}

/** Whether a read under `ticket` is still for this workspace user and scope order. */
function ticketCurrent(ticket: MacAnswerTicket): boolean {
  return ticket.userId === getCurrentUserId() && macReadCurrent(ticket.read);
}

/** Keep an answer read as `identity` and, from shells that prove it, as `provenViewer`. */
function acceptAnswerAs(
  ticket: MacAnswerTicket,
  identity: MacAccountIdentity | undefined,
  provenViewer: string | null,
  /** A saved answer checked against main's account: it confirms the reader but proves no read. */
  proof: { restored?: boolean } = {},
): MacAnswerVerdict {
  const entry = scopeState(ticket.provider, ticket.host);
  if (identity) {
    if (identity.generation < entry.generationFloor) {
      // Read before a change main reported: read again under the next order.
      advance(entry);
      return "moved";
    }
    const known = entry.identity;
    if (known && (known.login !== identity.login || known.generation !== identity.generation)) {
      // Read as another account than the one this scope shows.
      if (identity.generation > known.generation) {
        entry.identity = { login: identity.login, generation: identity.generation };
        advance(entry);
      }
      return "moved";
    }
    if (provenViewer !== null && !sameLogin(provenViewer, identity.login)) {
      // The credential read as someone other than the account main resolved.
      advance(entry);
      return "moved";
    }
  }
  // Older shells name no viewer; main's account stands in on GitHub.
  const reader = provenViewer ?? identity?.login ?? null;
  // A newer read as another reader is kept; the shared scope drops what was
  // cached as the previous one. A late one from before that change is refused.
  if (reader !== null && acceptMacRead(ticket.read, reader, proof) === "rejected") return "moved";
  if (identity && !entry.identity) {
    entry.identity = { login: identity.login, generation: identity.generation };
    // Only a read that went through clears a failure; a restore read nothing.
    if (!proof.restored) entry.failed = false;
    changed();
  }
  if (!identity) entry.failed = false;
  return "accepted";
}

/**
 * A read sent under `ticket` failed with `code`, so the next account change reads it again. When
 * main refused its account (signed out, expired or missing), what was read as it goes too.
 */
export function macReadFailed(ticket: MacAnswerTicket, code: string): void {
  scopeState(ticket.provider, ticket.host).failed = true;
  if (ticket.userId !== getCurrentUserId()) return;
  refuseMacReadSentAt(JSON.parse(ticket.read.scope) as MacReadScopeKey, ticket.read, code);
}

/**
 * The user saved a choice for `host`, as the shell answered it. Shown state
 * re-reads even when the account in use stays the same, e.g. Automatic to the
 * same account by name, and a different account moves the host's scope.
 */
export function macAccountChoiceSaved(host: string, choice: MacAccountChoice): void {
  accountsChanged(host, choice.effective?.generation ?? null);
}

/** The shell said the account changed mid-read: move the host's scope and re-read it. */
export function macAccountChanged(provider: MacReadProvider, host: string): void {
  if (provider === "github") accountChanged(host);
  // A device-read host has no account events: who reads is whoever the next read sees.
  else advance(scopeState(provider, host));
}

/**
 * Whether this Mac runs `operation` against `host` as its own identity: registered
 * device-read hosts, and GitHub hosts known to serve GitHub while the setting allows it.
 */
export function readsHostOnThisMac(
  provider: CodeHostPullRequest["provider"] | null,
  host: string | null,
  operation: string,
): boolean {
  if (!support || !provider || !host || !macOffers(provider, operation)) return false;
  if (!desktopReadsProvider(support, provider)) return false;
  if (provider !== "github") return namedCodeHost(host)?.provider === provider;
  const name = host.toLowerCase();
  return githubReadsEnabledNow() && (name === "github.com" || githubHosts.has(name));
}

/** Whether this shell runs `operation` for `provider` on the Mac, beyond the panel's basics. */
export function macOffers(provider: CodeHostPullRequest["provider"], operation: string): boolean {
  const host =
    provider === "github"
      ? support?.github
      : isDeviceReadProvider(provider)
        ? support?.named[provider]
        : null;
  return !!host?.operations.includes(operation);
}

/** The shell's read support, once known. */
export function desktopReadSupport(): DesktopReadSupport | null {
  return support;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  void loadSupport();
  return () => listeners.delete(listener);
}

/** Be told of any change to the read path or identities, outside React. */
export const subscribeDesktopReadPath = subscribe;

/** Re-render on any change to the read path or identities; returns the store's version. */
export function useDesktopReadPathVersion(): number {
  return useSyncExternalStore(
    subscribe,
    () => version,
    () => 0,
  );
}

/** Whether this shell can read GitHub PRs on the Mac, so the setting applies; live. */
export function useGitHubMacReadsAvailable(): boolean {
  useDesktopReadPathVersion();
  return !!support && desktopReadsProvider(support, "github");
}

/** Whether the shell has said what it reads; true at once in browsers. */
export function desktopReadSupportKnown(): boolean {
  return support !== null || !hasCodeHostBridge();
}

/** Deliver the shell's account-change event for `host`, as its bridge would. */
export function macAccountsChangedForTests(host: string, generation: number | null): void {
  accountsChanged(host, generation);
}

/** Forget everything, so a test can stub the bridge again. */
export function resetDesktopReadPathForTests(): void {
  stopWatching?.();
  stopWatching = null;
  identityRequests.clear();
  identityResolutions.clear();
  accountEpochs.clear();
  readPathLifetime += 1;
  identityCheckUsers.clear();
  support = null;
  supportRequest = null;
  automaticPolicy = false;
  githubHosts.clear();
  probedHosts.clear();
  unsignedHosts.clear();
  identityChecks.clear();
  scopes.clear();
  resetMacReadScopesForTests();
  scopeUser = null;
  readChoices.clear();
  sessionMachineBranches.clear();
  listeners.clear();
  version = 0;
  started = false;
  routeCache = null;
}
