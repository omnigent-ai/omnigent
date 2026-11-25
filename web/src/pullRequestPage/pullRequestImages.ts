// Images attached to a github.com pull request, read on this Mac: the shell reads the PR as
// this Mac's account and returns each attachment's bytes, kept here as in-memory blob URLs.
// An image belongs to one scope (workspace user, Mac account, PR, and the pages holding it);
// a copy whose scope is no longer current, or whose last page left, is revoked and never shown.

import { githubAttachmentAssetId } from "@/components/ai-elements/streamdown-security";
import type { CodeHostPullRequest } from "@/lib/codeHosts";
import { DesktopPullRequestError, readOnThisMac } from "@/lib/desktopPullRequests";
import {
  macIdentity,
  macOffers,
  readRouteFor,
  subscribeDesktopReadPath,
} from "@/lib/desktopReadPath";
import { getCurrentUserId, subscribeIdentity } from "@/lib/identity";
import { namedReaderEpoch, namedReadsAvailable, subscribeNamedReads } from "@/lib/macNamedReads";

export const PULL_REQUEST_IMAGES_OPERATION = "pull_request_images";
const MAX_ASSETS_PER_READ = 8;
const MAX_READS_IN_FLIGHT = 2;
const MAX_KEPT_IMAGES = 50;
const MAX_KEPT_BYTES = 64 * 1024 * 1024;

/** Why an attachment didn't load on this Mac; `not_found` means it isn't private. */
export type PullRequestImageFailure =
  "not_found" | "expired" | "too_large" | "unsupported_type" | "query_failed";

export type PullRequestImageResult =
  { kind: "loaded"; url: string } | { kind: "failed"; reason: PullRequestImageFailure };

interface ShellImage {
  asset: string;
  content_type?: string;
  bytes?: Uint8Array;
  error?: string;
}

const FAILURES = new Set<string>(["not_found", "expired", "too_large", "unsupported_type"]);

interface KeptImage {
  scope: string;
  prUrl: string;
  /** The Pull Requests page's own login it was read as, if any. */
  readAs?: string;
  url: string;
  size: number;
}

interface Waiter {
  scope: string;
  target: CodeHostPullRequest;
  asset: string;
  settle: (result: PullRequestImageResult) => void;
}

// Insertion order is recency: a hit moves its entry to the end.
const kept = new Map<string, KeptImage>();
let keptBytes = 0;
let queued: Waiter[] = [];
let inFlight = 0;
let flushScheduled = false;
let watching = false;

const keyFor = (scope: string, asset: string) => `${scope}\n${asset}`;

// Pages showing each PR, and how many times its last page left: a read started before
// then belongs to an older epoch, so its answer is never kept.
const holders = new Map<string, number>();
const epochs = new Map<string, number>();

/**
 * Hold `prUrl`'s images while a page shows it; the returned release, once every holder
 * has called it, revokes its copies and voids reads still in flight.
 */
export function retainPullRequestImages(prUrl: string): () => void {
  holders.set(prUrl, (holders.get(prUrl) ?? 0) + 1);
  let released = false;
  return () => {
    if (released) return;
    released = true;
    const left = (holders.get(prUrl) ?? 1) - 1;
    if (left > 0) {
      holders.set(prUrl, left);
      return;
    }
    holders.delete(prUrl);
    epochs.set(prUrl, (epochs.get(prUrl) ?? 0) + 1);
    dropPullRequestImages(prUrl);
  };
}

/** The scope a held page's images load in now, or null when no page holds the PR. */
function heldScope(target: CodeHostPullRequest | null): string | null {
  const scope = pullRequestImageScope(target);
  if (!scope || !target || !holders.has(target.url)) return null;
  return `${scope}\n${epochs.get(target.url) ?? 0}`;
}

/**
 * Where `prUrl`'s images load now: a scope key while the PR reads on this Mac from github.com
 * through a shell with the image read, else null. Any change of user, account or route changes it.
 */
export function pullRequestImageScope(target: CodeHostPullRequest | null): string | null {
  if (!target || target.provider !== "github" || target.host.toLowerCase() !== "github.com") {
    return null;
  }
  if (!macOffers("github", PULL_REQUEST_IMAGES_OPERATION)) return null;
  const route = readRouteFor(target.url);
  if (route.kind !== "mac" || route.target.url !== target.url) return null;
  if (target.readAs) {
    // Read as the page's own login: apart from the host account's copies, and gone with it.
    if (!namedReadsAvailable()) return null;
    const reader = { host: target.host, login: target.readAs };
    return [
      getCurrentUserId() ?? "",
      `as:${target.readAs.toLowerCase()}`,
      namedReaderEpoch(reader),
      target.url,
    ].join("\n");
  }
  const identity = macIdentity(target.host);
  return [
    getCurrentUserId() ?? "",
    identity?.login.toLowerCase() ?? "",
    identity?.generation ?? "",
    target.url,
  ].join("\n");
}

/** Whether this Mac's shell can load attachments for github.com PRs at all, whatever the route. */
export function macLoadsPullRequestImages(target: CodeHostPullRequest | null): boolean {
  return (
    target?.provider === "github" &&
    target.host.toLowerCase() === "github.com" &&
    macOffers("github", PULL_REQUEST_IMAGES_OPERATION)
  );
}

/** The loaded copy of `src` for `target`'s current scope, without reading. */
export function keptPullRequestImage(
  target: CodeHostPullRequest | null,
  src: string,
): string | null {
  const scope = heldScope(target);
  const asset = githubAttachmentAssetId(src);
  if (!scope || !asset) return null;
  const entry = kept.get(keyFor(scope, asset));
  if (!entry) return null;
  kept.delete(keyFor(scope, asset));
  kept.set(keyFor(scope, asset), entry);
  return entry.url;
}

/** Drop the loaded copy of `src`, as one that wouldn't show; the next load reads it again. */
export function forgetPullRequestImage(target: CodeHostPullRequest | null, src: string): void {
  const scope = heldScope(target);
  const asset = githubAttachmentAssetId(src);
  const entry = scope && asset ? kept.get(keyFor(scope, asset)) : undefined;
  if (entry) forget(keyFor(scope!, asset!), entry);
}

/**
 * Load the attachment `src` names for `target` on this Mac. Requests in one tick are batched
 * per PR; an answer that arrives after its scope changed settles `query_failed` unseen.
 */
export function loadPullRequestImage(
  target: CodeHostPullRequest,
  src: string,
): Promise<PullRequestImageResult> {
  watchScopes();
  const scope = heldScope(target);
  const asset = githubAttachmentAssetId(src);
  if (!scope || !asset) return Promise.resolve({ kind: "failed", reason: "query_failed" });
  const hit = keptPullRequestImage(target, src);
  if (hit) return Promise.resolve({ kind: "loaded", url: hit });
  return new Promise((settle) => {
    queued.push({ scope, target, asset, settle });
    scheduleFlush();
  });
}

/** Revoke every copy for `prUrl` and fail its queued loads, as its last page leaves. */
function dropPullRequestImages(prUrl: string): void {
  for (const [key, entry] of kept) {
    if (entry.prUrl === prUrl) forget(key, entry);
  }
  const dropped = queued.filter((waiter) => waiter.target.url === prUrl);
  queued = queued.filter((waiter) => waiter.target.url !== prUrl);
  for (const waiter of dropped) waiter.settle({ kind: "failed", reason: "query_failed" });
}

function scheduleFlush(): void {
  if (flushScheduled) return;
  flushScheduled = true;
  queueMicrotask(() => {
    flushScheduled = false;
    pump();
  });
}

// Start reads while there's room: up to 8 distinct assets of one scope per read.
function pump(): void {
  while (inFlight < MAX_READS_IN_FLIGHT && queued.length > 0) {
    const scope = queued[0].scope;
    const batch: Waiter[] = [];
    const assets = new Set<string>();
    const rest: Waiter[] = [];
    for (const waiter of queued) {
      if (waiter.scope !== scope) rest.push(waiter);
      else if (assets.has(waiter.asset)) batch.push(waiter);
      else if (assets.size < MAX_ASSETS_PER_READ) {
        assets.add(waiter.asset);
        batch.push(waiter);
      } else rest.push(waiter);
    }
    queued = rest;
    inFlight += 1;
    void readBatch(batch[0].target, scope, [...assets], batch).finally(() => {
      inFlight -= 1;
      pump();
    });
  }
}

async function readBatch(
  target: CodeHostPullRequest,
  scope: string,
  assets: string[],
  waiters: Waiter[],
): Promise<void> {
  let images: ShellImage[] = [];
  try {
    const { data } = await readOnThisMac<{ images?: ShellImage[] }>(
      target,
      PULL_REQUEST_IMAGES_OPERATION,
      { repository: target.repository, number: target.number, assets },
    );
    images = Array.isArray(data?.images) ? data.images : [];
  } catch (error) {
    // A refused read fails each image; anything else still settles them, then surfaces.
    if (!(error instanceof DesktopPullRequestError)) console.warn("[pullRequestImages]", error);
  }
  // The page may have left, or the reader changed, while this read ran.
  const current = heldScope(target) === scope;
  const results = new Map<string, PullRequestImageResult>();
  for (const asset of assets) {
    const image = images.find((entry) => entry.asset === asset);
    results.set(
      asset,
      current ? keep(scope, target.url, target.readAs, asset, image) : failed("query_failed"),
    );
  }
  for (const waiter of waiters) waiter.settle(results.get(waiter.asset)!);
}

const failed = (reason: PullRequestImageFailure): PullRequestImageResult => ({
  kind: "failed",
  reason,
});

function keep(
  scope: string,
  prUrl: string,
  readAs: string | undefined,
  asset: string,
  image: ShellImage | undefined,
): PullRequestImageResult {
  const key = keyFor(scope, asset);
  const existing = kept.get(key);
  if (existing) return { kind: "loaded", url: existing.url };
  if (!image?.bytes || !image.content_type?.startsWith("image/")) {
    const reason = image?.error && FAILURES.has(image.error) ? image.error : "query_failed";
    return failed(reason as PullRequestImageFailure);
  }
  const blob = new Blob([image.bytes as Uint8Array<ArrayBuffer>], { type: image.content_type });
  const entry = { scope, prUrl, readAs, url: URL.createObjectURL(blob), size: blob.size };
  kept.set(key, entry);
  keptBytes += entry.size;
  for (const [oldest, evicted] of kept) {
    if (kept.size <= MAX_KEPT_IMAGES && keptBytes <= MAX_KEPT_BYTES) break;
    if (oldest === key) continue;
    forget(oldest, evicted);
  }
  return { kind: "loaded", url: entry.url };
}

function forget(key: string, entry: KeptImage): void {
  kept.delete(key);
  keptBytes -= entry.size;
  URL.revokeObjectURL(entry.url);
}

// A change of user, account, route or support revokes every copy no longer in its scope.
function watchScopes(): void {
  if (watching) return;
  watching = true;
  const prune = () => {
    for (const [key, entry] of kept) {
      if (!isCurrentScope(entry)) forget(key, entry);
    }
  };
  subscribeDesktopReadPath(prune);
  subscribeIdentity(prune);
  subscribeNamedReads(prune);
}

function isCurrentScope(entry: KeptImage): boolean {
  const route = readRouteFor(entry.prUrl);
  return (
    route.kind === "mac" &&
    heldScope(entry.readAs ? { ...route.target, readAs: entry.readAs } : route.target) ===
      entry.scope
  );
}

export function resetPullRequestImagesForTests(): void {
  for (const [key, entry] of kept) forget(key, entry);
  queued = [];
  inFlight = 0;
  holders.clear();
  epochs.clear();
  flushScheduled = false;
}

export const PULL_REQUEST_IMAGE_LIMITS = {
  MAX_ASSETS_PER_READ,
  MAX_READS_IN_FLIGHT,
  MAX_KEPT_IMAGES,
  MAX_KEPT_BYTES,
};
