import { afterEach, describe, expect, it, vi } from "vitest";
import { NATIVE_CODING_AGENTS } from "@/lib/nativeCodingAgents";

// Mock the fetch used by fetchHarnessLabels.
const mockFetch = vi.fn();
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: (...args: unknown[]) => mockFetch(...args),
}));

// Import after mocking so the mock is in place.
const { useBrainHarnessLabels } = await import("@/lib/agentLabels");

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useBrainHarnessLabels", () => {
  it("excludes native TUI wrapper harnesses from the server catalog", async () => {
    // Simulate the server returning every harness, including native ones.
    const nativeHarnessRows = NATIVE_CODING_AGENTS.map((a) => ({
      id: a.harness as string,
      label: `Native ${a.displayName}`,
    }));
    const brainHarnessRow = { id: "my-community-harness", label: "My Harness" };

    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ data: [...nativeHarnessRows, brainHarnessRow] }),
    });

    // Render the hook via React Query.
    const { renderHook } = await import("@testing-library/react");
    const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
    const React = await import("react");

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: React.ReactNode }) =>
      React.createElement(QueryClientProvider, { client: queryClient }, children);

    const { result, unmount } = renderHook(() => useBrainHarnessLabels(), { wrapper });

    // Wait for the query to resolve.
    await vi.waitFor(() => {
      expect(result.current).toHaveProperty("my-community-harness");
    });

    const labels = result.current;

    // Brain harness (non-native) should be present.
    expect(labels["my-community-harness"]).toBe("My Harness");

    // All static BRAIN_HARNESS_LABELS should still be present.
    expect(labels["claude-sdk"]).toBe("Claude SDK");
    expect(labels["codex"]).toBe("Codex");
    expect(labels["cursor"]).toBe("Cursor");
    expect(labels["pi"]).toBe("Pi");
    expect(labels["antigravity"]).toBe("Antigravity");
    expect(labels["copilot"]).toBe("Copilot");

    // No native TUI wrapper should leak in.
    for (const agent of NATIVE_CODING_AGENTS) {
      expect(labels[agent.harness as string]).toBeUndefined();
    }

    unmount();
  });
});
