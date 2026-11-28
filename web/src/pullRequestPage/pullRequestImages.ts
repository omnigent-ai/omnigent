// Images attached to a github.com pull request, read through the session's host: the host reads
// the PR as its own account and streams each attachment's bytes, kept here as in-memory blob URLs.
// An image belongs to one scope (workspace user, session, PR, and the pages holding it); a copy
// whose scope is no longer current, or whose last page left, is revoked and never shown.

import { githubAttachmentAssetId } from "@/components/ai-elements/streamdown-security";
import { parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import { getCurrentUserId, subscribeIdentity } from "@/lib/identity";
import { asPullRequestApiError, PullRequestApiError } from "@/lib/pullRequestRpc";
import { callPullRequestMethod } from "@/lib/workspaceApi";

const MAX_READS_IN_FLIGHT = 2;
const MAX_KEPT_IMAGES = 50;
const MAX_KEPT_BYTES = 64 * 1024 * 1024;

/** Why an attachment didn't load through the host; `not_found` means it isn't private. */
export type PullRequestImageFailure =
  "not_found" | "expired" | "too_large" | "unsupported_type" | "query_failed";

export type PullRequestImageResult =
  { kind: "loaded"; url: string } | { kind: "failed"; reason: PullRequestImageFailure };

/** The PR whose images load, and the session whose host reads them. */
export interface PullRequestImageTarget {
  conversationId: string;
  prUrl: string;
}

interface KeptImage {
  scope: string;
  prUrl: string;
  url: string;
  size: number;
}

interface Waiter {
  scope: string;
  target: PullRequestImageTarget;
  src: string;
  settle: (result: PullRequestImageResult) => void;
}

// Insertion order is recency: a hit moves its entry to the end.
const kept = new Map<string, KeptImage>();
let keptBytes = 0;
let queued: Waiter[] = [];
let inFlight = 0;
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

/**
 * Where `target`'s images load now: a scope key for a github.com PR, else null. A change of
 * workspace user or session changes it.
 */
export function pullRequestImageScope(target: PullRequestImageTarget | null): string | null {
  if (!target) return null;
  const pullRequest = parseCodeHostPullRequestUrl(target.prUrl);
  if (pullRequest?.provider !== "github" || pullRequest.host.toLowerCase() !== "github.com") {
    return null;
  }
  return [getCurrentUserId() ?? "", target.conversationId, target.prUrl].join("\n");
}

/** The scope a held page's images load in now, or null when no page holds the PR. */
function heldScope(target: PullRequestImageTarget | null): string | null {
  const scope = pullRequestImageScope(target);
  if (!scope || !target || !holders.has(target.prUrl)) return null;
  return `${scope}\n${epochs.get(target.prUrl) ?? 0}`;
}

/** The loaded copy of `src` for `target`'s current scope, without reading. */
export function keptPullRequestImage(
  target: PullRequestImageTarget | null,
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
export function forgetPullRequestImage(target: PullRequestImageTarget | null, src: string): void {
  const scope = heldScope(target);
  const asset = githubAttachmentAssetId(src);
  const entry = scope && asset ? kept.get(keyFor(scope, asset)) : undefined;
  if (entry) forget(keyFor(scope!, asset!), entry);
}

/**
 * Load the attachment `src` names for `target` through the session's host. An answer that
 * arrives after its scope changed settles `query_failed` unseen.
 */
export function loadPullRequestImage(
  target: PullRequestImageTarget,
  src: string,
): Promise<PullRequestImageResult> {
  watchScopes();
  const scope = heldScope(target);
  if (!scope || !githubAttachmentAssetId(src)) return Promise.resolve(failed("query_failed"));
  const hit = keptPullRequestImage(target, src);
  if (hit) return Promise.resolve({ kind: "loaded", url: hit });
  return new Promise((settle) => {
    queued.push({ scope, target, src, settle });
    pump();
  });
}

/** Revoke every copy for `prUrl` and fail its queued loads, as its last page leaves. */
function dropPullRequestImages(prUrl: string): void {
  for (const [key, entry] of kept) {
    if (entry.prUrl === prUrl) forget(key, entry);
  }
  const dropped = queued.filter((waiter) => waiter.target.prUrl === prUrl);
  queued = queued.filter((waiter) => waiter.target.prUrl !== prUrl);
  for (const waiter of dropped) waiter.settle(failed("query_failed"));
}

function pump(): void {
  while (inFlight < MAX_READS_IN_FLIGHT && queued.length > 0) {
    const waiter = queued.shift()!;
    inFlight += 1;
    void readImage(waiter).finally(() => {
      inFlight -= 1;
      pump();
    });
  }
}

/** The image's bytes and type as the host streams them, or why it couldn't. */
async function streamImage(
  target: PullRequestImageTarget,
  src: string,
): Promise<{ type: string; bytes: Uint8Array } | PullRequestImageFailure> {
  try {
    const answer = await callPullRequestMethod(
      target.conversationId,
      "readPullRequestImage",
      async (client) => {
        const parts: Uint8Array[] = [];
        let type = "";
        for await (const message of client.readPullRequestImage({
          prUrl: target.prUrl,
          imageUrl: src,
        })) {
          if (message.metadata) type = message.metadata.contentType;
          parts.push(message.data);
        }
        const bytes = new Uint8Array(parts.reduce((total, part) => total + part.length, 0));
        let offset = 0;
        for (const part of parts) {
          bytes.set(part, offset);
          offset += part.length;
        }
        return { type, bytes };
      },
    );
    return answer ?? "query_failed";
  } catch (error) {
    const refused = asPullRequestApiError(error);
    if (refused instanceof PullRequestApiError && refused.status === 404) return "not_found";
    console.warn("[pullRequestImages]", error);
    return "query_failed";
  }
}

async function readImage(waiter: Waiter): Promise<void> {
  const answer = await streamImage(waiter.target, waiter.src);
  // The page may have left, or the workspace user changed, while this read ran.
  if (heldScope(waiter.target) !== waiter.scope) return waiter.settle(failed("query_failed"));
  if (typeof answer === "string") return waiter.settle(failed(answer));
  if (!answer.type.startsWith("image/")) return waiter.settle(failed("unsupported_type"));
  waiter.settle(keep(waiter, answer.type, answer.bytes));
}

const failed = (reason: PullRequestImageFailure): PullRequestImageResult => ({
  kind: "failed",
  reason,
});

function keep(waiter: Waiter, type: string, bytes: Uint8Array): PullRequestImageResult {
  const key = keyFor(waiter.scope, githubAttachmentAssetId(waiter.src)!);
  const existing = kept.get(key);
  if (existing) return { kind: "loaded", url: existing.url };
  const blob = new Blob([bytes as Uint8Array<ArrayBuffer>], { type });
  const entry = {
    scope: waiter.scope,
    prUrl: waiter.target.prUrl,
    url: URL.createObjectURL(blob),
    size: blob.size,
  };
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

// A change of workspace user revokes every copy no longer in its scope.
function watchScopes(): void {
  if (watching) return;
  watching = true;
  subscribeIdentity(() => {
    for (const [key, entry] of kept) {
      if (!entry.scope.startsWith(`${getCurrentUserId() ?? ""}\n`)) forget(key, entry);
    }
  });
}

export function resetPullRequestImagesForTests(): void {
  for (const [key, entry] of kept) forget(key, entry);
  queued = [];
  inFlight = 0;
  holders.clear();
  epochs.clear();
}

export const PULL_REQUEST_IMAGE_LIMITS = { MAX_READS_IN_FLIGHT, MAX_KEPT_IMAGES, MAX_KEPT_BYTES };
