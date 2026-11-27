import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import type { Conversation } from "@/hooks/useConversations";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { clearSessionMuteState, setMutedProjects } from "@/lib/sessionMute";

import { useSessionCommandFacts } from "./useSessionCommandFacts";

const filed: Conversation = {
  id: "conv_filed",
  object: "conversation",
  title: "Filed session",
  created_at: 0,
  updated_at: 0,
  labels: {},
  permission_level: null,
  project_id: "proj_1",
};

function Providers({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={new QueryClient()}>
      <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, session_mute: true }}>
        <MemoryRouter>{children}</MemoryRouter>
      </CapabilitiesProvider>
    </QueryClientProvider>
  );
}

afterEach(clearSessionMuteState);

describe("useSessionCommandFacts", () => {
  it("follows its project being muted and unmuted, apart from the session's own mute", () => {
    const { result } = renderHook(() => useSessionCommandFacts(filed), { wrapper: Providers });
    expect(result.current).toMatchObject({ muted: false, mutedByProject: false });

    act(() => setMutedProjects([{ id: "proj_1", name: "Payments", muted: true }], null));
    expect(result.current).toMatchObject({ muted: false, mutedByProject: true });

    act(() => setMutedProjects([], null));
    expect(result.current).toMatchObject({ muted: false, mutedByProject: false });
  });
});
