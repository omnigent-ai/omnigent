// Seeded sequences of everything that changes a conversations page: first pages (complete,
// warning, or replacing the history), more threads, more replies, created conversations, edits,
// deletes and GitHub changing meanwhile. After every step the page is checked against an
// independent oracle that records which observation supplied each part shown.

import { describe, expect, it } from "vitest";

import type {
  ReviewComment,
  ReviewThread,
  ReviewThreadsPage,
} from "@/lib/pullRequestReviewThreads";
import {
  type CachedThreads,
  reconcileThreads,
  withChangedComment,
  withMoreComments,
  withMoreThreads,
  withWrittenThread,
} from "@/lib/reviewThreadsHistory";
import { oldestReadAt, readsComplete } from "@/lib/reviewThreadsReadAt";

const HEAD = "b".repeat(40);
const BASE = "a".repeat(40);
/** Threads per page, and replies per page, as GitHub pages them here. */
const PAGE = 2;

/** A small deterministic generator, so every failure names a seed that replays it. */
function random(seed: number) {
  let value = seed >>> 0;
  return () => {
    value = (value + 0x6d2b79f5) >>> 0;
    let mixed = Math.imul(value ^ (value >>> 15), 1 | value);
    mixed = (mixed + Math.imul(mixed ^ (mixed >>> 7), 61 | mixed)) ^ mixed;
    return ((mixed ^ (mixed >>> 14)) >>> 0) / 4294967296;
  };
}

interface ServerThread {
  id: string;
  comments: { id: string; body: string }[];
}

/** GitHub's conversations as they stand, paged as GitHub pages them. */
class Server {
  threads: ServerThread[] = [];
  private next = 0;

  id(prefix: string) {
    this.next += 1;
    return `${prefix}${this.next}`;
  }

  private threadOf(value: ServerThread, from: number): ReviewThread {
    const comments = value.comments.slice(from, from + PAGE);
    const more = from + PAGE < value.comments.length;
    return {
      id: value.id,
      path: "src/app.ts",
      line: 1,
      is_resolved: false,
      comments: {
        object: "list",
        data: comments.map((item) => ({ ...item, author: "carol" }) as unknown as ReviewComment),
        has_more: more,
        next_cursor: more ? `${value.id}:${from + PAGE}` : null,
      },
    } as unknown as ReviewThread;
  }

  threadsPage(from: number, warning?: string): ReviewThreadsPage {
    const listed = this.threads.slice(from, from + PAGE);
    const more = from + PAGE < this.threads.length;
    return {
      object: "list",
      data: listed.map((value) => this.threadOf(value, 0)),
      has_more: more,
      next_cursor: more ? `threads:${from + PAGE}` : null,
      head_sha: HEAD,
      base_sha: BASE,
      ...(warning ? { warning } : {}),
    } as unknown as ReviewThreadsPage;
  }

  replies(cursor: string) {
    const [id, from] = cursor.split(":");
    const value = this.threads.find((item) => item.id === id);
    if (!value) return { object: "list", data: [], has_more: false, next_cursor: null };
    const answer = this.threadOf(value, Number(from)).comments;
    return answer;
  }

  thread(id: string) {
    const value = this.threads.find((item) => item.id === id);
    return value && this.threadOf(value, 0);
  }
}

interface Source {
  at: number;
}

/**
 * Which observation supplied each part, never pruned: what the page shows picks the entries
 * that count. Written from the observations alone, not from the page's reads.
 */
class Oracle {
  sources = new Map<string, Source>();

  static parts(page: ReviewThreadsPage): string[] {
    const parts = ["#listing"];
    for (const value of page.data) {
      parts.push(`#thread ${value.id}`);
      for (const item of value.comments.data) parts.push(`#comment ${value.id} ${item.id}`);
    }
    return parts;
  }

  observe(threads: readonly ReviewThread[], at: number, listing: boolean) {
    if (listing) this.sources.set("#listing", { at });
    for (const value of threads) {
      this.sources.set(`#thread ${value.id}`, { at });
      for (const item of value.comments.data) {
        this.sources.set(`#comment ${value.id} ${item.id}`, { at });
      }
    }
  }

  observeReplies(threadId: string, comments: readonly { id: string }[], at: number) {
    for (const item of comments) this.sources.set(`#comment ${threadId} ${item.id}`, { at });
  }

  counted(page: ReviewThreadsPage): Source[] {
    const parts = Oracle.parts(page);
    const counted = page.data.length === 0 ? ["#listing"] : parts.slice(1);
    return counted.map((part) => {
      const source = this.sources.get(part);
      if (!source) throw new Error(`no observation supplied ${part}`);
      return source;
    });
  }

  age(page: ReviewThreadsPage) {
    return Math.min(...this.counted(page).map((source) => source.at));
  }
}

type Step =
  | "first"
  | "first-warning"
  | "first-empty-warning"
  | "replace-history"
  | "more-threads"
  | "more-replies"
  | "github-adds-thread"
  | "github-adds-reply"
  | "github-deletes-reply"
  | "github-deletes-thread"
  | "write-thread"
  | "edit"
  | "delete";

const STEPS: readonly Step[] = [
  "first",
  "first",
  "first-warning",
  "first-empty-warning",
  "replace-history",
  "more-threads",
  "more-threads",
  "more-replies",
  "more-replies",
  "github-adds-thread",
  "github-adds-reply",
  "github-adds-reply",
  "github-deletes-reply",
  "github-deletes-thread",
  "write-thread",
  "edit",
  "delete",
];

/** One sequence: the steps it ran, failing with the seed and the steps so far. */
function runSequence(seed: number, length: number) {
  const next = random(seed);
  const pick = <T>(values: readonly T[]) => values[Math.floor(next() * values.length)]!;
  const server = new Server();
  for (let count = Math.floor(next() * 5); count > 0; count -= 1) {
    const replies = Array.from({ length: Math.floor(next() * 4) + 1 }, () => ({
      id: server.id("c"),
      body: "reply",
    }));
    server.threads.push({ id: server.id("t"), comments: replies });
  }
  const oracle = new Oracle();
  let now = 1_700_000_000_000;
  let page: CachedThreads | undefined;
  const ran: string[] = [];

  const check = (step: string) => {
    if (!page) return;
    const where = `seed ${seed}, after ${[...ran, step].join(", ")}`;
    expect(readsComplete(page), where).toBe(true);
    expect(Object.keys(page.reads!).length, where).toBe(Oracle.parts(page).length);
    expect(oldestReadAt(page), where).toBe(oracle.age(page));
  };

  const firstPage = (warning?: string, empty = false) => {
    const answer = empty
      ? ({ ...server.threadsPage(0), data: [], warning } as ReviewThreadsPage)
      : server.threadsPage(0, warning);
    return { ...answer, read_as: "alice" };
  };

  for (let index = 0; index < length; index += 1) {
    now += Math.floor(next() * 10 * 60_000) + 1;
    const step = page ? pick(STEPS) : "first";
    switch (step) {
      case "first":
      case "first-warning":
      case "first-empty-warning":
      case "replace-history": {
        const warning = step === "first" || step === "replace-history" ? undefined : "partial";
        const answer = firstPage(warning, step === "first-empty-warning");
        const previous = step === "replace-history" ? undefined : page;
        const result = reconcileThreads(answer, previous, now);
        // A warning alone, over a loaded page, observes nothing.
        if (!(answer.warning && answer.data.length === 0 && previous)) {
          oracle.observe(answer.data, now, !answer.warning);
        }
        page = result;
        break;
      }
      case "more-threads": {
        if (!page!.next_cursor) continue;
        const from = Number(page!.next_cursor.split(":")[1]);
        const answer = server.threadsPage(from);
        page = withMoreThreads(page!, answer, now);
        oracle.observe(answer.data, now, false);
        break;
      }
      case "more-replies": {
        const open = page!.data.filter((value) => value.comments.next_cursor);
        if (open.length === 0) continue;
        const target = pick(open);
        const answer = server.replies(target.comments.next_cursor!) as never as {
          data: ReviewComment[];
          has_more: boolean;
          next_cursor: string | null;
        };
        page = withMoreComments(page!, target, answer as never, now);
        oracle.observeReplies(target.id, answer.data, now);
        break;
      }
      case "github-adds-thread":
        server.threads.unshift({
          id: server.id("t"),
          comments: [{ id: server.id("c"), body: "new" }],
        });
        ran.push(step);
        continue;
      case "github-adds-reply": {
        if (server.threads.length === 0) continue;
        pick(server.threads).comments.push({ id: server.id("c"), body: "reply" });
        ran.push(step);
        continue;
      }
      case "github-deletes-thread": {
        if (server.threads.length === 0) continue;
        server.threads.splice(Math.floor(next() * server.threads.length), 1);
        ran.push(step);
        continue;
      }
      case "github-deletes-reply": {
        const withReplies = server.threads.filter((value) => value.comments.length > 1);
        if (withReplies.length === 0) continue;
        const target = pick(withReplies);
        target.comments.splice(Math.floor(next() * target.comments.length), 1);
        ran.push(step);
        continue;
      }
      case "write-thread": {
        const created = { id: server.id("t"), comments: [{ id: server.id("c"), body: "posted" }] };
        server.threads.push(created);
        const answer = server.thread(created.id)!;
        page = withWrittenThread(page!, answer, now);
        oracle.observe([answer], now, false);
        break;
      }
      case "edit":
      case "delete": {
        const shownComments = page!.data.flatMap((value) =>
          value.comments.data.map((item) => ({ threadId: value.id, commentId: item.id })),
        );
        if (shownComments.length === 0) continue;
        const { threadId, commentId } = pick(shownComments);
        const owned = server.threads.find((value) => value.id === threadId);
        if (step === "delete" && owned) {
          owned.comments = owned.comments.filter((item) => item.id !== commentId);
          if (owned.comments.length === 0) server.threads.splice(server.threads.indexOf(owned), 1);
        }
        const change = (item: ReviewComment) =>
          step === "edit" ? { ...item, body: `${item.body}, edited` } : null;
        page = withChangedComment(page!, threadId, commentId, change) as CachedThreads;
        break;
      }
    }
    check(step);
    ran.push(step);
  }
  return ran;
}

describe("conversations page reads, over seeded sequences", () => {
  it.each(Array.from({ length: 300 }, (_, index) => index + 1))(
    "agrees with the oracle after every step: seed %i",
    (seed) => {
      runSequence(seed, 40);
    },
  );

  it("covers every step", () => {
    const counts = new Map<string, number>();
    for (let seed = 1; seed <= 300; seed += 1) {
      const ran = runSequence(seed, 40);
      for (const step of ran) counts.set(step, (counts.get(step) ?? 0) + 1);
    }
    for (const step of new Set(STEPS)) {
      expect(counts.get(step) ?? 0, step).toBeGreaterThan(10);
    }
  });
});
