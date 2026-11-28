// Ready for review, Convert to draft and Disable auto-merge on a Pull Requests list's rows: which
// rows offer what. A row's changes are made on GitHub, so each one that applies hands off there.

import type * as IdentityModule from "@/lib/identity";
import type * as SonnerModule from "sonner";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { currentUser, identityListeners, toast, toastError } = vi.hoisted(() => ({
  currentUser: { id: "user-1" as string | null },
  identityListeners: new Set<() => void>(),
  toast: vi.fn(),
  toastError: vi.fn(),
}));
vi.mock("sonner", async (importOriginal) => ({
  ...(await importOriginal<typeof SonnerModule>()),
  toast: Object.assign(toast, { success: vi.fn(), error: toastError, dismiss: vi.fn() }),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => currentUser.id,
  subscribeIdentity: (listener: () => void) => {
    identityListeners.add(listener);
    return () => identityListeners.delete(listener);
  },
}));

import {
  type PullRequestActionOrigin,
  PullRequestActionOriginContext,
} from "@/lib/pullRequestActionOrigin";
import type { RowAction } from "@/shell/PullRequestListRows";

import { resetLifecycleActionsForTests } from "./pullRequestLifecycleActions";
import { resetStateChainsForTests } from "./pullRequestStateChain";
import {
  type LifecycleRow,
  rowLifecycleAvailability,
  useRowLifecycleActions,
} from "./useRowLifecycleActions";
import { EXAMPLE_DEVICE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// Example Host stand-in hosts are recognized by name, as a build that registers Example Host does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_DEVICE_HOST] });

const PR = "https://github.com/acme/app/pull/42";
const HEAD = "a".repeat(40);
const SESSION = "pull-requests-page";

function row(pr: Partial<NonNullable<LifecycleRow["pr"]>> | null = {}): LifecycleRow {
  return {
    url: PR,
    number: 42,
    paletteName: "#42 Fix the build",
    pr: pr === null ? undefined : { state: "OPEN", is_draft: false, head_sha: HEAD, ...pr },
  };
}

/** The rows the list shows now. */
const listed = new Map<string, LifecycleRow>();
function renderRows(
  /** The Pull Requests page the list is on; null for a list elsewhere. */
  origin: PullRequestActionOrigin | null = null,
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <PullRequestActionOriginContext.Provider value={origin}>
        {children}
      </PullRequestActionOriginContext.Provider>
    </QueryClientProvider>
  );
  return renderHook(
    () => {
      const rows = useRowLifecycleActions(SESSION, true, listed);
      // Each row asked for is what the list shows now.
      return {
        actionsFor: (shown: LifecycleRow) => {
          listed.set(shown.url, shown);
          return rows.actionsFor(shown);
        },
      };
    },
    { wrapper: wrapper },
  );
}

beforeEach(() => {
  listed.clear();
  currentUser.id = "user-1";
  identityListeners.clear();
  resetLifecycleActionsForTests();
  resetStateChainsForTests();
  toast.mockReset();
  toastError.mockReset();
});

afterEach(() => {
  resetLifecycleActionsForTests();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

describe("which rows offer what", () => {
  const facts = { held: false };

  it("offers Ready for review on an open draft and Convert to draft on an open, ready PR", () => {
    expect(rowLifecycleAvailability("ready_for_review", row({ is_draft: true }), facts)).toEqual({
      kind: "github",
      url: PR,
    });
    expect(rowLifecycleAvailability("convert_to_draft", row({ is_draft: true }), facts)).toEqual({
      kind: "hidden",
    });
    expect(rowLifecycleAvailability("convert_to_draft", row(), facts)).toEqual({
      kind: "github",
      url: PR,
    });
    expect(rowLifecycleAvailability("ready_for_review", row(), facts)).toEqual({ kind: "hidden" });
  });

  it.each([
    ["closed", { state: "CLOSED" }],
    ["merged", { state: "MERGED" }],
    ["a closed draft", { state: "CLOSED", is_draft: true }],
  ])("offers neither on a %s PR", (_name, pr) => {
    for (const id of ["ready_for_review", "convert_to_draft"] as const) {
      expect(rowLifecycleAvailability(id, row(pr), facts)).toEqual({ kind: "hidden" });
    }
  });

  it("offers nothing while the row's read hasn't loaded", () => {
    expect(rowLifecycleAvailability("convert_to_draft", row(null), facts)).toEqual({
      kind: "hidden",
    });
  });

  it("offers nothing on another provider's PR, auto-merge included, and GitHub elsewhere", () => {
    const elsewhere = { held: false, onGitHub: false };
    expect(rowLifecycleAvailability("convert_to_draft", row(), elsewhere)).toEqual({
      kind: "hidden",
    });
    expect(rowLifecycleAvailability("enable_auto_merge", row(), elsewhere)).toEqual({
      kind: "hidden",
    });
    expect(
      rowLifecycleAvailability("enable_auto_merge", row(), { ...elsewhere, onGitHub: undefined }),
    ).toEqual({ kind: "github", url: PR });
  });
});

describe("a Example Host row's changes", () => {
  it("shows each change unavailable, saying where to make it, and links nowhere", () => {
    const { result } = renderRows();
    const deviceHostUrl = "https://2.host.example.com/example-org/universe/pull/20002346";
    const actions: RowAction[] = result.current.actionsFor({
      ...row({ is_draft: true }),
      url: deviceHostUrl,
    });
    const ready = actions.find((action) => action.id === "lifecycle:ready_for_review");
    expect(ready?.availability).toEqual(
      expect.objectContaining({ reason: "Change this pull request on Example Host." }),
    );
    // The row's own Open on Example Host takes it there; these add no link of their own.
    expect(actions.every((action) => action.href === undefined)).toBe(true);
  });
});
