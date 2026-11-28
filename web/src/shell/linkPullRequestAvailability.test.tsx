import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type * as SessionsApiModule from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";

import { CHECKING_ACCESS_REASON, CommandContextProvider } from "./commandContext";
import {
  LINK_PULL_REQUEST_NEEDS_EDIT,
  useLinkPullRequestAvailability,
} from "./linkPullRequestAvailability";

// A session whose snapshot hasn't been seeded stays loading.
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  getSessionSlim: () => new Promise(() => {}),
}));

function snapshot(id: string, permissionLevel: number | null): Session {
  return { id, permissionLevel, parentSessionId: null } as unknown as Session;
}

/** Renders the hook for `conv_pane`, with `levels` seeded as loaded snapshots. */
function availabilityFor(
  levels: Record<string, number | null>,
  routedSession: string | null = null,
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  for (const [id, level] of Object.entries(levels)) {
    client.setQueryData(["session", id], snapshot(id, level));
  }
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      {routedSession === null ? (
        children
      ) : (
        <CommandContextProvider
          session={{
            id: routedSession,
            permissionLevel: 4,
            permissionResolved: true,
            topLevel: true,
            archived: false,
          }}
          view="session"
          workspaceTab={null}
          filePath={null}
        >
          {children}
        </CommandContextProvider>
      )}
    </QueryClientProvider>
  );
  return renderHook(() => useLinkPullRequestAvailability("conv_pane"), { wrapper }).result.current;
}

describe("useLinkPullRequestAvailability", () => {
  it("reads the target session's own snapshot, with or without a routed session", () => {
    expect(availabilityFor({ conv_pane: 2 })).toEqual({ available: true });
    // A grid pane under another routed session the viewer owns.
    expect(availabilityFor({ conv_pane: 2 }, "conv_routed")).toEqual({ available: true });
    // Single-user servers report no level, which grants access.
    expect(availabilityFor({ conv_pane: null })).toEqual({ available: true });
  });

  it("greys out a read-only target even when the routed session is owned", () => {
    expect(availabilityFor({ conv_pane: 1, conv_routed: 4 }, "conv_routed")).toEqual({
      available: false,
      reason: LINK_PULL_REQUEST_NEEDS_EDIT,
    });
  });

  it("stays unconfirmed until the target's snapshot loads", () => {
    expect(availabilityFor({ conv_routed: 4 }, "conv_routed")).toEqual({
      available: false,
      reason: CHECKING_ACCESS_REASON,
    });
  });
});
