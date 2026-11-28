import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ReviewThread } from "@/lib/pullRequestReviewThreads";
import type { ReviewThreadContext } from "@/newPullRequestPage/shell/PullRequestReviewThread";
import { useStableReviewThreadContext } from "./useStableReviewThreadContext";

const thread = { id: "thread-1" } as ReviewThread;

function contextWith(overrides: Partial<ReviewThreadContext> = {}): ReviewThreadContext {
  return {
    conversationId: "session-1",
    prUrl: "https://example.com/acme/app/pull/1",
    account: "octocat",
    canReply: true,
    canComment: true,
    canCreate: false,
    headSha: "head",
    baseSha: "base",
    loadComments: vi.fn(),
    loadingComments: null,
    ...overrides,
  };
}

describe("useStableReviewThreadContext", () => {
  it("keeps one identity while a parent rebuilds the same values with new callbacks", () => {
    const { result, rerender } = renderHook((context) => useStableReviewThreadContext(context), {
      initialProps: contextWith({ showInChanges: vi.fn() }),
    });
    const first = result.current;
    rerender(contextWith({ showInChanges: vi.fn() }));
    expect(result.current).toBe(first);
  });

  it("runs the latest callbacks through the kept identity", () => {
    const { result, rerender } = renderHook((context) => useStableReviewThreadContext(context), {
      initialProps: contextWith(),
    });
    const loadComments = vi.fn();
    const showInChanges = vi.fn();
    rerender(contextWith({ loadComments, showInChanges }));
    result.current.loadComments(thread);
    result.current.showInChanges?.(thread);
    expect(loadComments).toHaveBeenCalledWith(thread);
    expect(showInChanges).toHaveBeenCalledWith(thread);
  });

  it("changes when a value changes, and offers Show in Changes only when the parent does", () => {
    const { result, rerender } = renderHook((context) => useStableReviewThreadContext(context), {
      initialProps: contextWith(),
    });
    const first = result.current;
    expect(first.showInChanges).toBeUndefined();

    rerender(contextWith({ loadingComments: "thread-1" }));
    expect(result.current).not.toBe(first);
    expect(result.current.loadingComments).toBe("thread-1");

    rerender(contextWith({ loadingComments: "thread-1", showInChanges: vi.fn() }));
    expect(result.current.showInChanges).toBeTypeOf("function");
  });
});
